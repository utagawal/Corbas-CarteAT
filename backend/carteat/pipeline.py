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
            self._idx = StreetIndex(osm.load(path))
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
            # Géocodages interrompus par une panne réseau lors d'un passage précédent : on les reprend.
            retry = [r.id for r in existing.values()
                     if not r.modifie_manuellement and (r.extraction or {}).get("geocodage_incomplet")]
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
    def process(self, acte: Acte) -> None:
        if not acte.pdf_url:
            raise ValueError("Aucun PDF associé à l'acte")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", acte.id)
        pdf = self.s.pdf_dir / f"{safe}.pdf"
        if not pdf.exists():
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
            self.apply_extraction(row, mode=mode)

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


def build_title(objet: str | None, categorie: str, lieu: str | None, particulier: bool, numero: str) -> str:
    label = CATEGORIES.get(categorie, "Arrêté")
    if particulier or categorie == "demenagement" or not objet:
        return f"{label} – {lieu}" if lieu else label
    t = anonymize(objet)
    t = re.sub(r"\s*Voies?\s+M[ée]tropoles?\.?\s*$", "", t, flags=re.I)
    t = re.sub(r"^ARR[ÊE]T[ÉE]\s+TEMPORAIRE\s*[-–:]?\s*", "", t, flags=re.I)
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
