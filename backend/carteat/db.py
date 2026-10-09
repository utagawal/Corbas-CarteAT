"""Persistance SQLite (SQLAlchemy 2)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Arrete(Base):
    __tablename__ = "arretes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # identifiant de l'acte au registre
    numero: Mapped[str] = mapped_column(String(32), index=True)  # « AT 242/26 » tel que publié
    objet_registre: Mapped[str] = mapped_column(Text, default="")  # non publié (peut contenir des noms)
    date_decision: Mapped[str | None] = mapped_column(String(10))
    date_publication: Mapped[str | None] = mapped_column(String(10))
    pdf_url: Mapped[str | None] = mapped_column(Text)
    pdf_file: Mapped[str | None] = mapped_column(Text)

    texte: Mapped[str] = mapped_column(Text, default="")  # texte OCR/natif (non publié)
    extraction: Mapped[dict] = mapped_column(JSON, default=dict)  # résultat brut du parseur

    # Champs publiés (modifiables par l'administrateur)
    titre: Mapped[str] = mapped_column(Text, default="")
    categorie: Mapped[str] = mapped_column(String(32), default="autre")
    impacts: Mapped[list] = mapped_column(JSON, default=list)
    date_debut: Mapped[str | None] = mapped_column(String(10), index=True)
    date_fin: Mapped[str | None] = mapped_column(String(10), index=True)
    calendrier: Mapped[dict] = mapped_column(JSON, default=dict)
    lieu: Mapped[str] = mapped_column(Text, default="")
    intervenant: Mapped[str] = mapped_column(Text, default="")
    deviation: Mapped[list] = mapped_column(JSON, default=list)
    geojson: Mapped[dict] = mapped_column(JSON, default=dict)
    centre: Mapped[list | None] = mapped_column(JSON)
    qualite_geo: Mapped[str] = mapped_column(String(16), default="none")
    note: Mapped[str] = mapped_column(Text, default="")  # remarque publique libre

    # Cycle de vie
    statut: Mapped[str] = mapped_column(String(16), default="a_verifier", index=True)  # publie | a_verifier | masque
    motif_verification: Mapped[str] = mapped_column(Text, default="")
    modifie_manuellement: Mapped[bool] = mapped_column(Boolean, default=False)
    erreur: Mapped[str] = mapped_column(Text, default="")
    tentatives: Mapped[int] = mapped_column(Integer, default=0)
    # Suivi de l'acte au registre : empreinte des métadonnées (détection des modifications) et,
    # s'il a disparu du registre, date du retrait et statut/motif à restaurer s'il réapparaît.
    suivi_registre: Mapped[dict | None] = mapped_column(JSON, default=dict)
    cree_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    maj_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KV(Base):
    """Cache clé/valeur (géocodage…) et petits états (dernière synchro)."""

    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    maj_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SyncLog(Base):
    __tablename__ = "sync_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    debut: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    fin: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declencheur: Mapped[str] = mapped_column(String(32), default="planifie")
    nouveaux: Mapped[int] = mapped_column(Integer, default=0)
    erreurs: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(self.engine, "connect")
        def _pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def _add_missing_columns(self) -> None:
        """Migration minimale : create_all ne crée pas les colonnes ajoutées à une table existante."""
        with self.engine.begin() as c:
            for table in Base.metadata.sorted_tables:
                have = {r[1] for r in c.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
                for col in table.columns:
                    if col.name not in have:
                        c.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                                          f'{col.type.compile(self.engine.dialect)}')

    @contextmanager
    def session(self):
        s: Session = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # --- cache KV
    def kv_get(self, key: str):
        with self.session() as s:
            row = s.get(KV, key)
            return json.loads(row.value) if row else None

    def kv_set(self, key: str, value) -> None:
        with self.session() as s:
            row = s.get(KV, key)
            if row:
                row.value = json.dumps(value, ensure_ascii=False)
            else:
                s.add(KV(key=key, value=json.dumps(value, ensure_ascii=False)))

    def known_ids(self) -> set[str]:
        with self.session() as s:
            return set(s.scalars(select(Arrete.id)))


class KVCache:
    """Adaptateur pour le géocodeur."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, key):
        return self.db.kv_get(key)

    def set(self, key, value):
        self.db.kv_set(key, value)
