"""Synchronisation : arrêtés retirés ou modifiés au registre."""

from datetime import date
from pathlib import Path

import pytest

from carteat import pipeline as pl
from carteat.config import Settings
from carteat.db import Arrete, Database
from carteat.registry import Acte

FIX = Path(__file__).parent / "fixtures"
TODAY = date.today().isoformat()


class FakeRegistry:
    def __init__(self):
        self.actes: list[Acte] = []
        self.downloads: list[str] = []

    def list_at(self, numero_regex, start_year):
        return list(self.actes)

    def download(self, url, dest):
        self.downloads.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 test")
        return dest


def acte(i: int, doc: str = "doc1", objet: str = "Travaux") -> Acte:
    return Acte(id=f"id{i}", numero=f"AT {i}/26", objet=objet, date_decision=TODAY, date_publication=TODAY,
                pdf_url=f"https://registre.test/actes/id{i}/{doc}", pdf_name="at.pdf", pdf_taille="100 Ko")


@pytest.fixture()
def p(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, static_dir=tmp_path / "nofront", secret_key="x" * 40, sync_on_startup=False)
    pipe = pl.Pipeline(s, Database(tmp_path / "t.db"))
    pipe.registry = FakeRegistry()
    pipe.ensure_osm = lambda force=False: "test"
    texte = (FIX / "at_242_26.txt").read_text(encoding="utf-8")
    monkeypatch.setattr(pl, "pdf_to_text", lambda *a, **k: (texte, "natif"))

    class Geo:
        def geocode(self, *a, **k):
            return {"geojson": {"type": "FeatureCollection", "features": []}, "quality": "exact", "center": [4.9, 45.67]}

    pipe.geocoder = lambda: Geo()
    return pipe


def row(p, i: int) -> Arrete:
    with p.db.session() as ses:
        return ses.get(Arrete, f"id{i}")


def test_retrait_puis_reapparition(p):
    p.registry.actes = [acte(i) for i in range(1, 11)]
    p.sync()
    assert row(p, 3).statut == "publie"
    p.registry.actes = [a for a in p.registry.actes if a.id != "id3"]
    r = p.sync()
    assert "1 retiré(s) du registre" in r["message"]
    r3 = row(p, 3)
    assert r3.statut == "masque" and r3.motif_verification.startswith("Retiré du registre")
    # un nouveau calcul (ex. reprise du géocodage) ne le republie pas
    p.reprocess("id3")
    assert row(p, 3).statut == "masque"
    p.registry.actes.append(acte(3))
    p.sync()
    r3 = row(p, 3)
    assert r3.statut == "publie" and r3.motif_verification == "" and "retire_le" not in r3.suivi_registre


def test_reapparition_arrete_corrige_a_la_main(p):
    p.registry.actes = [acte(i) for i in range(1, 11)]
    p.sync()
    with p.db.session() as ses:
        r = ses.get(Arrete, "id4")
        r.statut, r.motif_verification, r.modifie_manuellement = "a_verifier", "à revoir", True
    p.registry.actes = [a for a in p.registry.actes if a.id != "id4"]
    p.sync()
    assert row(p, 4).statut == "masque"
    p.registry.actes.append(acte(4))
    p.sync()
    r4 = row(p, 4)
    assert (r4.statut, r4.motif_verification) == ("a_verifier", "à revoir")


def test_disparition_massive_ignoree(p):
    p.registry.actes = [acte(i) for i in range(1, 11)]
    p.sync()
    p.registry.actes = p.registry.actes[:2]  # registre incomplet : 8 absents sur 10
    r = p.sync()
    assert "ignoré" in r["message"]
    assert all(row(p, i).statut == "publie" for i in range(1, 11))


def test_modification_au_registre(p):
    p.registry.actes = [acte(1), acte(2)]
    p.sync()
    assert len(p.registry.downloads) == 2
    # acte 1 : nouveau document ; acte 2 corrigé à la main puis modifié au registre
    with p.db.session() as ses:
        r2 = ses.get(Arrete, "id2")
        r2.date_fin, r2.modifie_manuellement = "2026-12-31", True
    p.registry.actes = [acte(1, doc="doc2"), acte(2, objet="Travaux prolongés")]
    r = p.sync()
    assert "2 modifié(s) au registre" in r["message"]
    assert p.registry.downloads[-2:] == ["https://registre.test/actes/id1/doc2", "https://registre.test/actes/id2/doc1"]
    assert row(p, 1).statut == "publie"
    r2 = row(p, 2)
    assert r2.statut == "a_verifier" and "modifié au registre" in r2.motif_verification
    assert r2.date_fin == "2026-12-31"  # correction manuelle conservée
    # arrêté masqué volontairement par l'administrateur : reste masqué s'il est modifié au registre
    with p.db.session() as ses:
        ses.get(Arrete, "id1").statut = "masque"
        ses.get(Arrete, "id1").modifie_manuellement = True
    p.registry.actes[0] = acte(1, doc="doc3")
    p.sync()
    assert row(p, 1).statut == "masque"
    # rien de nouveau : pas de retraitement
    n = len(p.registry.downloads)
    p.sync()
    assert len(p.registry.downloads) == n


def test_base_existante_sans_empreinte(p):
    """Après mise à jour de l'application, les arrêtés déjà en base ne sont pas tous retraités."""
    p.registry.actes = [acte(1)]
    p.sync()
    with p.db.session() as ses:
        ses.get(Arrete, "id1").suivi_registre = None
    n = len(p.registry.downloads)
    p.sync()
    assert len(p.registry.downloads) == n
    assert row(p, 1).suivi_registre["signature"] == acte(1).signature()


def test_signature_independante_de_l_hote():
    a, b = acte(1), acte(1)
    b.pdf_url = "https://autre-hote.test/api/v2/actes/id1/doc1"
    assert a.signature() == b.signature()


def test_migration_colonne_ajoutee(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE arretes (id VARCHAR(64) PRIMARY KEY, numero VARCHAR(32))")
    con.commit()
    con.close()
    Database(db)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(arretes)")}
    assert "suivi_registre" in cols and "statut" in cols
