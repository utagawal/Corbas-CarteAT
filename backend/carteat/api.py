"""Application web : API publique, API d'administration et fichiers statiques du front."""

from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import shape
from sqlalchemy import desc, func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .db import Arrete, Database, SyncLog
from .geocode import geometry_center
from .parser import CATEGORIES, IMPACTS, extract_locations, remove_postal_addresses
from .pipeline import Pipeline, status_for
from .security import RateLimiter, SecurityHeaders, client_ip, verify_password

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------- modèles d'entrée
class Login(BaseModel):
    password: str = Field(max_length=200)


class Update(BaseModel):
    titre: str | None = Field(None, max_length=300)
    categorie: str | None = None
    impacts: list[str] | None = None
    date_debut: date | None = None
    date_fin: date | None = None
    horaires: list[str] | None = None
    lieu: str | None = Field(None, max_length=500)
    intervenant: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=1000)
    statut: Literal["publie", "a_verifier", "masque"] | None = None
    geojson: dict | None = None


class Reprocess(BaseModel):
    ocr: bool = False


class GeoText(BaseModel):
    texte: str = Field(max_length=2000)


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    s = settings or get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(s.db_path)
    pipeline = Pipeline(s, db)
    limiter = RateLimiter()
    scheduler = BackgroundScheduler(timezone=s.timezone)

    def run_sync(trigger: str):
        threading.Thread(target=pipeline.sync, args=(trigger,), daemon=True, name="sync").start()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            scheduler.add_job(pipeline.sync, CronTrigger(hour=s.sync_hour, minute=s.sync_minute, timezone=s.timezone),
                              args=("planifie",), id="sync", max_instances=1, coalesce=True, misfire_grace_time=3600)
            scheduler.start()
            if s.sync_on_startup:
                with db.session() as ses:
                    last = ses.scalars(select(SyncLog).order_by(desc(SyncLog.debut)).limit(1)).first()
                stale = last is None or (datetime.now(timezone.utc) - _aware(last.debut)) > timedelta(hours=20)
                if stale:
                    run_sync("demarrage")
        yield
        if scheduler.running:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Carte des arrêtés travaux – Corbas", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.pipeline, app.state.settings = db, pipeline, s

    secret = s.secret_key or secrets.token_urlsafe(32)
    if not s.secret_key:
        log.warning("SECRET_KEY absente : les sessions d'administration seront perdues à chaque redémarrage.")
    app.add_middleware(SecurityHeaders, map_style_url=s.map_style_url, frame_ancestors=s.frame_ancestors)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="carteat_admin", max_age=8 * 3600,
                       same_site="strict", https_only=s.cookie_secure)

    # ------------------------------------------------------------------ public
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz():
        return {"ok": True}

    @app.api_route("/api/meta", methods=["GET", "HEAD"])
    def meta():
        idx = pipeline.idx
        with db.session() as ses:
            last = ses.scalars(select(SyncLog).where(SyncLog.fin.is_not(None)).order_by(desc(SyncLog.fin)).limit(1)).first()
        b = idx.data.get("boundary")
        bounds = shape(b).bounds if b else None
        return {
            "commune": s.commune_nom,
            "boundary": b,
            "bounds": bounds,
            "map_style_url": s.map_style_url,
            "categories": CATEGORIES,
            "impacts": IMPACTS,
            "derniere_synchro": last.fin.isoformat() if last else None,
            "registre_url": s.registry_page_url,
            "osm_date": idx.data.get("generated"),
        }

    @app.api_route("/api/arretes", methods=["GET", "HEAD"])
    def list_public(periode: Literal["actuels", "tous"] = "actuels"):
        today = _today(s)
        with db.session() as ses:
            q = select(Arrete).where(Arrete.statut == "publie")
            if periode == "actuels":
                q = q.where(or_(Arrete.date_fin >= today.isoformat(), Arrete.date_fin.is_(None)))
            rows = ses.scalars(q.order_by(Arrete.date_debut)).all()
        return JSONResponse([public_view(r, today) for r in rows], headers={"Cache-Control": "public, max-age=60"})

    # ------------------------------------------------------------------ administration
    def require_admin(request: Request):
        if not request.session.get("admin"):
            raise HTTPException(401, "Authentification requise")
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get("x-requested-with") != "carteat":
            raise HTTPException(403, "Requête refusée")
        return True


    @app.post("/api/admin/login")
    def login(body: Login, request: Request):
        if request.headers.get("x-requested-with") != "carteat":
            raise HTTPException(403, "Requête refusée")
        if not (s.admin_password or s.admin_password_hash):
            raise HTTPException(503, "Aucun mot de passe administrateur n'est configuré (ADMIN_PASSWORD_HASH).")
        if not limiter.allow(client_ip(request)):
            raise HTTPException(429, "Trop de tentatives, réessayez dans 15 minutes.")
        if not verify_password(body.password, s.admin_password_hash, s.admin_password):
            raise HTTPException(401, "Mot de passe incorrect")
        request.session.clear()
        request.session["admin"] = True
        return {"ok": True}

    @app.post("/api/admin/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/admin/me")
    def me(request: Request):
        return {"admin": bool(request.session.get("admin"))}

    @app.get("/api/admin/arretes", dependencies=[Depends(require_admin)])
    def admin_list(statut: str | None = None):
        today = _today(s)
        with db.session() as ses:
            q = select(Arrete)
            if statut:
                q = q.where(Arrete.statut == statut)
            rows = ses.scalars(q.order_by(desc(Arrete.date_publication), desc(Arrete.numero))).all()
            counts = dict(ses.execute(select(Arrete.statut, func.count()).group_by(Arrete.statut)).all())
        return {"compteurs": counts, "arretes": [admin_view(r, today, full=False) for r in rows]}

    @app.get("/api/admin/arretes/{arrete_id}", dependencies=[Depends(require_admin)])
    def admin_get(arrete_id: str):
        with db.session() as ses:
            r = ses.get(Arrete, arrete_id)
            if not r:
                raise HTTPException(404)
            return admin_view(r, _today(s), full=True)


    @app.put("/api/admin/arretes/{arrete_id}", dependencies=[Depends(require_admin)])
    def admin_update(arrete_id: str, body: Update):
        data = body.model_dump(exclude_unset=True)
        if "categorie" in data and data["categorie"] not in CATEGORIES:
            raise HTTPException(422, "Catégorie inconnue")
        if "impacts" in data and any(i not in IMPACTS for i in data["impacts"]):
            raise HTTPException(422, "Mesure inconnue")
        if data.get("geojson") is not None:
            _validate_fc(data["geojson"])
        with db.session() as ses:
            r = ses.get(Arrete, arrete_id)
            if not r:
                raise HTTPException(404)
            content_change = False
            for k, v in data.items():
                if k == "horaires":
                    r.calendrier = {**(r.calendrier or {}), "horaires": v}
                    content_change = True
                elif k in ("date_debut", "date_fin"):
                    setattr(r, k, v.isoformat() if v else None)
                    content_change = True
                elif k == "geojson":
                    r.geojson = v
                    r.centre = geometry_center(v)
                    r.qualite_geo = "manuel" if v.get("features") else "none"
                    content_change = True
                elif k != "statut":
                    setattr(r, k, v)
                    content_change = True
            if "statut" in data:
                r.statut = data["statut"]
                if r.statut == "publie":
                    r.motif_verification = ""
            if content_change or "statut" in data:
                # Toute décision de l'administrateur (y compris masquer/publier) prime sur les
                # retraitements automatiques ultérieurs (reprise de géocodage, `cli reprocess`).
                r.modifie_manuellement = True
            if r.date_debut and r.date_fin and r.date_fin < r.date_debut:
                raise HTTPException(422, "La date de fin précède la date de début")
            return admin_view(r, _today(s), full=True)


    @app.post("/api/admin/arretes/{arrete_id}/retraiter", dependencies=[Depends(require_admin)])
    def admin_reprocess(arrete_id: str, body: Reprocess):
        try:
            pipeline.reprocess(arrete_id, ocr=body.ocr, force=True)
        except KeyError:
            raise HTTPException(404) from None
        with db.session() as ses:
            return admin_view(ses.get(Arrete, arrete_id), _today(s), full=True)


    @app.post("/api/admin/geocoder", dependencies=[Depends(require_admin)])
    def admin_geocode(body: GeoText):
        locs, cited = extract_locations(remove_postal_addresses(body.texte), pipeline.idx)
        if not locs:
            locs = [{"kind": "street", "street": c} for c in cited]
        res = pipeline.geocoder().geocode(locs, [])
        return {"localisations": locs, **res}

    @app.get("/api/admin/rues", dependencies=[Depends(require_admin)])
    def admin_streets():
        return sorted(pipeline.idx.geoms)

    @app.post("/api/admin/sync", dependencies=[Depends(require_admin)])
    def admin_sync():
        if pipeline.running:
            return {"statut": "deja_en_cours"}
        run_sync("manuel")
        return {"statut": "lance"}

    @app.get("/api/admin/sync", dependencies=[Depends(require_admin)])
    def admin_sync_status():
        with db.session() as ses:
            logs = ses.scalars(select(SyncLog).order_by(desc(SyncLog.debut)).limit(15)).all()
            return {"en_cours": pipeline.running, "journal": [
                {"debut": l.debut.isoformat(), "fin": l.fin.isoformat() if l.fin else None, "declencheur": l.declencheur,
                 "nouveaux": l.nouveaux, "erreurs": l.erreurs, "message": l.message} for l in logs]}

    @app.get("/api/admin/arretes/{arrete_id}/pdf", dependencies=[Depends(require_admin)])
    def admin_pdf(arrete_id: str):
        with db.session() as ses:
            r = ses.get(Arrete, arrete_id)
        if not r or not r.pdf_file:
            raise HTTPException(404)
        path = s.pdf_dir / r.pdf_file.split("/")[-1]
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="application/pdf")

    # ------------------------------------------------------------------ front
    if s.static_dir.exists():
        app.mount("/", StaticFiles(directory=s.static_dir, html=True), name="front")
    else:
        log.warning("Front introuvable (%s) : seule l'API est servie.", s.static_dir)
    return app


# ---------------------------------------------------------------------- sérialisation
def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _today(s: Settings) -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(s.timezone)).date()


def public_view(r: Arrete, today: date) -> dict:
    return {
        "id": r.id,
        "numero": r.numero,
        "titre": r.titre,
        "categorie": r.categorie,
        "impacts": r.impacts or [],
        "date_debut": r.date_debut,
        "date_fin": r.date_fin,
        "jours": (r.calendrier or {}).get("jours", []),
        "horaires": (r.calendrier or {}).get("horaires", []),
        "remarques": (r.calendrier or {}).get("remarques", []),
        "lieu": r.lieu,
        "intervenant": r.intervenant,
        "deviation": r.deviation or [],
        "note": r.note,
        "centre": r.centre,
        "geojson": r.geojson or {"type": "FeatureCollection", "features": []},
        "etat": status_for(r, today),
        "pdf_url": r.pdf_url,
        "date_publication": r.date_publication,
    }


def admin_view(r: Arrete, today: date, full: bool) -> dict:
    d = public_view(r, today)
    d.update({
        "statut": r.statut,
        "motif_verification": r.motif_verification,
        "qualite_geo": r.qualite_geo,
        "modifie_manuellement": r.modifie_manuellement,
        "erreur": r.erreur,
        "objet_registre": r.objet_registre,
        "maj_le": r.maj_le.isoformat() if r.maj_le else None,
    })
    if not full:
        d.pop("geojson")
    else:
        d["texte"] = r.texte
        d["extraction"] = r.extraction
    return d


def _validate_fc(fc: dict) -> None:
    if fc.get("type") != "FeatureCollection" or not isinstance(fc.get("features"), list) or len(fc["features"]) > 50:
        raise HTTPException(422, "GeoJSON invalide")
    for f in fc["features"]:
        try:
            g = shape(f["geometry"])
        except Exception:  # noqa: BLE001
            raise HTTPException(422, "Géométrie invalide") from None
        if g.geom_type not in ("Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon"):
            raise HTTPException(422, "Type de géométrie non pris en charge")
        f.setdefault("properties", {})
        if f["properties"].get("role") not in ("impact", "deviation"):
            f["properties"]["role"] = "impact"
