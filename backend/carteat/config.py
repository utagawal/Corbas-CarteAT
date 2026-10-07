"""Configuration par variables d'environnement (voir .env.example)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Serveur
    port: int = 8080
    data_dir: Path = Path("/data")
    static_dir: Path = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    public_url: str = ""  # ex. https://maps.utagawavtt.com/ATCorbas/ (liens absolus, sitemap)

    # Registre PubliS²low
    registry_api: str = "https://publis2low.adullact.org/api/v1"
    registry_collectivite_id: str = "ed3f7f7f-1ec9-4d61-8cb8-8def9c0f95c3"
    registry_page_url: str = "https://publis2low.adullact.org/registre/216902734"
    numero_regex: str = r"^\s*AT\s*\d+\s*/\s*\d+\s*$"
    start_year: int = 2026  # premier import : arrêtés publiés à partir de cette année

    # Commune
    commune_nom: str = "Corbas"
    commune_insee: str = "69273"
    commune_postcode: str = "69960"
    # sud, ouest, nord, est (zone de téléchargement OSM, un peu plus large que la commune)
    commune_bbox: tuple[float, float, float, float] = (45.645, 4.875, 45.685, 4.935)

    # Services externes
    ban_url: str = "https://api-adresse.data.gouv.fr"
    overpass_urls: list[str] = [
        "https://overpass-api.de/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    ]
    osm_refresh_days: int = 30
    map_style_url: str = "https://tiles.openfreemap.org/styles/positron"
    user_agent: str = "CarteAT-Corbas/1.0 (+https://github.com/utagawal/Corbas-CarteAT)"

    # OCR
    ocr_lang: str = "fra"
    ocr_dpi: int = 300

    # Planification (heure de Paris)
    sync_hour: int = 6
    sync_minute: int = 15
    sync_on_startup: bool = True
    timezone: str = "Europe/Paris"

    # Administration
    admin_password: str = ""  # mot de passe en clair (déconseillé) …
    admin_password_hash: str = ""  # … ou empreinte scrypt générée par `python -m carteat.cli hash-password`
    secret_key: str = ""  # clé de signature des sessions (obligatoire en production)
    cookie_secure: bool = True

    # Publication automatique
    auto_publish_max_days: int = 366  # au-delà : arrêté « permanent » → à vérifier

    # Intégration (iframe) : domaines autorisés à afficher la carte
    frame_ancestors: str = "'self' https://corbas.fr https://*.corbas.fr"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "carteat.sqlite3"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdf"

    @property
    def osm_path(self) -> Path:
        return self.data_dir / "osm_corbas.json.gz"


@lru_cache
def get_settings() -> Settings:
    return Settings()
