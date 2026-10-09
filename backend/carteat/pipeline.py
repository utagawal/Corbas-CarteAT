"""Chaîne de traitement : registre → PDF → texte → extraction → géocodage → base."""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select

from . import osm
from .config import Settings
from .db import Arrete, Database, KVCache, SyncLog, utcnow
from .geocode import BanClient, Geocoder
from .parser import CATEGORIES, anonymize, parse
from .registry import Acte, Registry
from .streets import StreetIndex
from .textract import pdf_to_text

log = logging.getLogger(__name__)

MAX_TENTATIVES = 3
# Garde-fou : au-delà de cette part d'arrêtés « disparus » d'un coup, on suppose une réponse
# incomplète du registre plutôt qu'un retrait réel, et on ne masque rien.
RETRAIT_MAX_PART = 0.2
RETRAIT_MAX_MIN = 5
# Les actes quittent le registre à la fin de leur période de publication (3 ans) : ce n'est
# pas un retrait, on ne suit donc les disparitions que pour les actes plus récents.
RETRAIT_SUIVI_JOURS = 900


class Pipeline:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.registry = Registry(settings.registry_api, settings.registry_collectivite_id, settings.user_agent)
        self._idx: StreetIndex | None = None
        self._lock = threading.Lock()
        self.running = False

    # ------------------------------------------------------------------ référentiel OSM
    def ensure_osm(self, force: bool = False) -> str:
        p = self.s.osm_path
        fresh = p.exists() and (time.time() - p.stat().st_mtime) < self.s.osm_refresh_days * 86400
        if fresh and not force:
            return "à jour"
        try:
            data = osm.fetch_overpass(self.s.overpass_urls, self.s.commune_insee, self.s.commune_bbox, self.s.user_agent)
            osm.save(data, p)
            self._idx = None
            return f"mis à jour ({len(data['ways'])} tronçons)"
        except Exception as e:  # noqa: BLE001
            log.warning("Mise à jour OSM impossible, utilisation des données existantes : %s", e)
            return f"échec de la mise à jour ({e})"

    @property
    def idx(self) -> StreetIndex:
        if self._idx is None:
            path = self.s.osm_path if self.s.osm_path.exists() else osm.SEED_PATH
            self._idx = StreetIndex(osm.load(path), osm.load_lieux(self.s.data_dir / "lieux.json"))
        return self._idx

    def geocoder(self) -> Geocoder:
        ban = BanClient(self.s.ban_url, self.s.commune_insee, self.s.commune_postcode, KVCache(self.db), self.s.user_agent)
        return Geocoder(self.idx, ban)

    # ------------------------------------------------------------------ synchronisation
    def sync(self, trigger: str = "planifie") -> dict:
        if not self._lock.acquire(blocking=False):
            return {"statut": "deja_en_cours"}
        self.running = True
        logrow = SyncLog(declencheur=trigger)
        with self.db.session() as ses:
            ses.add(logrow)
        nouveaux = erreurs = 0
        msg = []
        try:
            msg.append("OSM : " + self.ensure_osm())
            actes = self.registry.list_at(self.s.numero_regex, self.s.start_year)
            with self.db.session() as ses:
                existing = {a.id: a for a in ses.scalars(select(Arrete))}
            todo = [a for a in actes if a.id not in existing
                    or (existing[a.id].erreur and existing[a.id].tentatives < MAX_TENTATIVES)]
            msg.append(f"{len(actes)} AT au registre, {len(todo)} à traiter")
            todo_ids = {a.id for a in todo}
            modifies, info = self.suivre_registre(actes, exclure=todo_ids)
            msg.extend(info)
            # Géocodages interrompus par une panne réseau lors d'un passage précédent : on les reprend.
            retry = [r.id for r in existing.values()
                     if not r.modifie_manuellement and (r.extraction or {}).get("geocodage_incomplet")
                     and not (r.suivi_registre or {}).get("retire_le")]
            for rid in retry:
                try:
                    self.reprocess(rid)
                except Exception:  # noqa: BLE001
                    log.exception("Reprise du géocodage impossible pour %s", rid)
            if retry:
                msg.append(f"{len(retry)} géocodage(s) repris")
            log.info(msg[-1])
            for acte in sorted(todo, key=lambda a: a.date_publication or ""):
                try:
                    self.process(acte)
                    nouveaux += 1
                except Exception as e:  # noqa: BLE001 — un document en erreur ne bloque pas les autres
                    erreurs += 1
                    log.exception("Échec du traitement de %s", acte.numero)
                    self._store_error(acte, str(e))
            for acte in modifies:
                try:
                    self.process(acte, maj_registre=True)
                except Exception:  # noqa: BLE001 — l'empreinte n'est pas mise à jour : nouvel essai au prochain passage
                    erreurs += 1
                    log.exception("Échec de la mise à jour de %s modifié au registre", acte.numero)
        except Exception as e:  # noqa: BLE001
            erreurs += 1
            msg.append(f"Erreur : {e}")
            log.exception("Synchronisation interrompue")
        finally:
            with self.db.session() as ses:
                row = ses.get(SyncLog, logrow.id)
                row.fin, row.nouveaux, row.erreurs, row.message = utcnow(), nouveaux, erreurs, " | ".join(msg)
            self.running = False
            self._lock.release()
        return {"statut": "ok", "nouveaux": nouveaux, "erreurs": erreurs, "message": " | ".join(msg)}

    def suivre_registre(self, actes: list[Acte], exclure: set[str] = frozenset()) -> tuple[list[Acte], list[str]]:
        """Compare la base au registre : masque les arrêtés retirés, rétablit ceux qui réapparaissent
        et renvoie les arrêtés modifiés au registre (empreinte différente) à retraiter."""
        par_id = {a.id: a for a in actes}
        today = datetime.now(timezone.utc).date()
        modifies: list[Acte] = []
        info: list[str] = []
        with self.db.session() as ses:
            rows = list(ses.scalars(select(Arrete)))
            disparus = [r for r in rows if r.id not in par_id and not (r.suivi_registre or {}).get("retire_le")
                        and _suivi_retrait(r, self.s.start_year, today)]
            limite = max(RETRAIT_MAX_MIN, int(RETRAIT_MAX_PART * len(rows)))
            if disparus and (not actes or len(disparus) > limite):
                log.warning("%d arrêtés absents du registre : réponse probablement incomplète, aucun masquage", len(disparus))
                info.append(f"{len(disparus)} absent(s) du registre ignoré(s) (réponse incomplète ?)")
                disparus = []
            for r in disparus:
                r.suivi_registre = {**(r.suivi_registre or {}), "retire_le": today.isoformat(),
                                    "statut_avant": r.statut, "motif_avant": r.motif_verification}
                r.statut = "masque"
                r.motif_verification = f"Retiré du registre officiel (constaté le {today.strftime('%d/%m/%Y')})"
                log.info("%s retiré du registre : masqué", r.numero)
            if disparus:
                info.append(f"{len(disparus)} retiré(s) du registre, masqué(s)")
            revenus = 0
            for r in rows:
                a = par_id.get(r.id)
                if a is None or r.id in exclure:
                    continue
                suivi = dict(r.suivi_registre or {})
                if suivi.get("retire_le"):
                    # Réapparu : on rétablit l'état d'avant, sauf si l'administrateur a statué entre-temps.
                    if r.statut == "masque" and r.motif_verification.startswith("Retiré du registre"):
                        r.statut = suivi.get("statut_avant") or "a_verifier"
                        r.motif_verification = suivi.get("motif_avant") or ""
                    for k in ("retire_le", "statut_avant", "motif_avant"):
                        suivi.pop(k, None)
                    revenus += 1
                if not suivi.get("signature"):
                    suivi["signature"] = a.signature()  # premier passage : on prend l'état actuel comme référence
                elif suivi["signature"] != a.signature():
                    modifies.append(a)
                r.suivi_registre = suivi
            if revenus:
                info.append(f"{revenus} réapparu(s) au registre")
            if modifies:
                info.append(f"{len(modifies)} modifié(s) au registre, retraité(s)")
        return modifies, info

    def _store_error(self, acte: Acte, err: str) -> None:
        with self.db.session() as ses:
            row = ses.get(Arrete, acte.id)
            if row is None:
                row = Arrete(id=acte.id, numero=acte.numero, objet_registre=acte.objet,
                             date_decision=acte.date_decision, date_publication=acte.date_publication,
                             pdf_url=acte.pdf_url, titre=acte.numero, statut="a_verifier")
                ses.add(row)
            row.erreur = err[:2000]
            row.tentatives = (row.tentatives or 0) + 1
            row.motif_verification = "Erreur de traitement automatique"

    # ------------------------------------------------------------------ traitement d'un acte
    def process(self, acte: Acte, maj_registre: bool = False) -> None:
        """Traite un acte. `maj_registre` : l'acte, déjà connu, a été modifié au registre ; on
        retélécharge le document et, s'il avait été corrigé à la main, on le signale à vérifier
        plutôt que d'écraser les corrections."""
        if not acte.pdf_url:
            raise ValueError("Aucun PDF associé à l'acte")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", acte.id)
        pdf = self.s.pdf_dir / f"{safe}.pdf"
        if maj_registre or not pdf.exists():
            self.registry.download(acte.pdf_url, pdf)
        texte, mode = pdf_to_text(pdf, self.s.ocr_lang, self.s.ocr_dpi)
        with self.db.session() as ses:
            row = ses.get(Arrete, acte.id)
            if row is None:
                row = Arrete(id=acte.id)
                ses.add(row)
            row.numero = acte.numero
            row.objet_registre = acte.objet
            row.date_decision = acte.date_decision
            row.date_publication = acte.date_publication
            row.pdf_url = acte.pdf_url
            row.pdf_file = str(pdf)
            row.texte = texte
            row.erreur = ""
            row.tentatives = (row.tentatives or 0) + 1
            row.suivi_registre = {**(row.suivi_registre or {}), "signature": acte.signature()}
            self.apply_extraction(row, mode=mode)
            if maj_registre and row.modifie_manuellement and not (row.suivi_registre or {}).get("retire_le"):
                row.statut = "a_verifier"
                row.motif_verification = ("Arrêté modifié au registre officiel : vérifier les corrections manuelles "
                                          "(dates, rues, tracé) par rapport au nouveau document")

    def apply_extraction(self, row: Arrete, mode: str = "", regeocode: bool = True) -> None:
        """(Re)calcule les champs publiés à partir du texte, sauf s'ils ont été modifiés à la main."""
        ref = None
        if row.date_decision:
            try:
                ref = date.fromisoformat(row.date_decision)
            except ValueError:
                pass
        ex = parse(row.texte or "", self.idx, ref)
        exd = ex.to_dict()
        exd["methode_texte"] = mode or (row.extraction or {}).get("methode_texte", "")
        row.extraction = exd
        if row.modifie_manuellement:
            return
        geo = self.geocoder().geocode(ex.localisations, ex.deviation) if regeocode else {
            "geojson": row.geojson, "quality": row.qualite_geo, "center": row.centre}
        cal = ex.calendrier
        row.categorie = ex.categorie
        row.impacts = ex.impacts
        row.calendrier = cal
        row.date_debut = cal.get("debut")
        row.date_fin = cal.get("fin") or cal.get("debut")
        row.lieu = ex.lieu_texte or ""
        row.deviation = ex.deviation
        row.intervenant = "Particulier" if ex.particulier else (ex.entreprise or "")
        row.extraction = {**exd, "geocodage_incomplet": bool(geo.get("incomplete"))}
        row.geojson = geo["geojson"]
        row.centre = geo["center"]
        row.qualite_geo = geo["quality"]
        row.titre = build_title(ex.objet, ex.categorie, ex.lieu_texte, ex.particulier, row.numero)
        if (row.suivi_registre or {}).get("retire_le"):
            return  # retiré du registre : reste masqué même après un nouveau calcul
        statut, motif = self.decide_status(row, ex)
        row.statut, row.motif_verification = statut, motif

    def decide_status(self, row: Arrete, ex) -> tuple[str, str]:
        motifs = []
        if not row.date_debut:
            motifs.append("dates non identifiées")
        if not row.impacts:
            motifs.append("aucune mesure de circulation/stationnement identifiée")
        if row.qualite_geo in ("none", "approx"):
            motifs.append("localisation " + ("absente" if row.qualite_geo == "none" else "approximative"))
        if row.date_debut and row.date_fin:
            d = (date.fromisoformat(row.date_fin) - date.fromisoformat(row.date_debut)).days
            if d > self.s.auto_publish_max_days:
                motifs.append(f"durée inhabituelle ({d} jours)")
        if not ex.numero:
            motifs.append("format de document inhabituel")
        return ("a_verifier", " ; ".join(motifs)) if motifs else ("publie", "")

    # ------------------------------------------------------------------ outils d'administration
    def reprocess(self, arrete_id: str, ocr: bool = False, force: bool = False) -> None:
        with self.db.session() as ses:
            row = ses.get(Arrete, arrete_id)
            if row is None:
                raise KeyError(arrete_id)
            if ocr and row.pdf_file and Path(row.pdf_file).exists():
                row.texte, mode = pdf_to_text(Path(row.pdf_file), self.s.ocr_lang, self.s.ocr_dpi)
            if force:
                row.modifie_manuellement = False
            self.apply_extraction(row)


def _suivi_retrait(row: Arrete, start_year: int, today: date) -> bool:
    """Vrai si la disparition de cet arrêté du registre doit être traitée comme un retrait."""
    pub = row.date_publication or row.date_decision
    if not pub:
        return False
    try:
        d = date.fromisoformat(pub)
    except ValueError:
        return False
    return d.year >= start_year and (today - d).days < RETRAIT_SUIVI_JOURS


def build_title(objet: str | None, categorie: str, lieu: str | None, particulier: bool, numero: str) -> str:
    label = CATEGORIES.get(categorie, "Arrêté")
    if particulier or categorie == "demenagement" or not objet:
        return f"{label} – {lieu}" if lieu else label
    t = anonymize(objet)
    t = re.sub(r"\s*Voies?\s+M[ée]tropoles?\.?\s*$", "", t, flags=re.I)
    t = re.sub(r"^ARR[ÊE]T[ÉE]\s+TEMPORAIRE\s*[-–:]?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*-{2,}\s*|\s+[-–—]\s+", " – ", t)  # « Echafaudage -- 68 av. » → « Echafaudage – 68 av. »
    t = t.strip(" -–/,.")
    if len(t) > 160:
        t = t[:157].rsplit(" ", 1)[0] + "…"
    return t[:1].upper() + t[1:] if t else label


def status_for(row: Arrete, today: date | None = None) -> str:
    """« en_cours », « a_venir » ou « termine » par rapport à la date du jour."""
    today = today or datetime.now(timezone.utc).date()
    if not row.date_debut:
        return "inconnu"
    deb = date.fromisoformat(row.date_debut)
    fin = date.fromisoformat(row.date_fin) if row.date_fin else deb
    if fin < today:
        return "termine"
    if deb > today:
        return "a_venir"
    return "en_cours"
