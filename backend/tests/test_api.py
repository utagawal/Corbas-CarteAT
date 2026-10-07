from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from carteat.api import create_app
from carteat.config import Settings
from carteat.db import Arrete
from carteat.security import hash_password, verify_password


@pytest.fixture()
def client(tmp_path):
    s = Settings(data_dir=tmp_path, static_dir=tmp_path / "nofront", admin_password_hash=hash_password("motdepasse-test-123"),
                 secret_key="x" * 40, cookie_secure=False, sync_on_startup=False)
    app = create_app(s, start_scheduler=False)
    today = date.today()
    with app.state.db.session() as ses:
        ses.add_all([
            Arrete(id="a1", numero="AT 1/26", titre="Travaux en cours", categorie="voirie", impacts=["route_barree"],
                   date_debut=(today - timedelta(days=2)).isoformat(), date_fin=(today + timedelta(days=2)).isoformat(),
                   statut="publie", objet_registre="Déménagement M. SECRET", texte="texte privé"),
            Arrete(id="a2", numero="AT 2/26", titre="Terminé", categorie="voirie", impacts=["stationnement"],
                   date_debut="2026-01-01", date_fin="2026-01-02", statut="publie"),
            Arrete(id="a3", numero="AT 3/26", titre="Non vérifié", categorie="autre", impacts=[],
                   date_debut=today.isoformat(), date_fin=today.isoformat(), statut="a_verifier"),
        ])
    return TestClient(app)


def test_public_list_filters_and_privacy(client):
    r = client.get("/api/arretes")
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()]
    assert ids == ["a1"]
    a = r.json()[0]
    assert a["etat"] == "en_cours"
    assert "objet_registre" not in a and "texte" not in a  # données internes jamais exposées
    assert "Content-Security-Policy" in r.headers
    tous = [a["id"] for a in client.get("/api/arretes?periode=tous").json()]
    assert set(tous) == {"a1", "a2"}


def test_admin_requires_login_and_csrf_header(client):
    assert client.get("/api/admin/arretes").status_code == 401
    assert client.post("/api/admin/login", json={"password": "motdepasse-test-123"}).status_code == 403
    h = {"X-Requested-With": "carteat"}
    assert client.post("/api/admin/login", json={"password": "faux"}, headers=h).status_code == 401
    assert client.post("/api/admin/login", json={"password": "motdepasse-test-123"}, headers=h).status_code == 200
    data = client.get("/api/admin/arretes").json()
    assert data["compteurs"] == {"publie": 2, "a_verifier": 1}
    # modification sans en-tête anti-CSRF refusée
    assert client.put("/api/admin/arretes/a3", json={"statut": "publie"}).status_code == 403
    r = client.put("/api/admin/arretes/a3", json={"statut": "publie", "impacts": ["stationnement"],
                                                  "geojson": {"type": "FeatureCollection", "features": [
                                                      {"type": "Feature", "properties": {},
                                                       "geometry": {"type": "Point", "coordinates": [4.9, 45.67]}}]}},
                   headers=h)
    assert r.status_code == 200
    assert r.json()["modifie_manuellement"] is True and r.json()["centre"] == [4.9, 45.67]
    assert {a["id"] for a in client.get("/api/arretes").json()} == {"a1", "a3"}


def test_admin_rejects_bad_values(client):
    h = {"X-Requested-With": "carteat"}
    client.post("/api/admin/login", json={"password": "motdepasse-test-123"}, headers=h)
    assert client.put("/api/admin/arretes/a1", json={"categorie": "inconnue"}, headers=h).status_code == 422
    assert client.put("/api/admin/arretes/a1", json={"date_debut": "2026-05-10", "date_fin": "2026-05-01"},
                      headers=h).status_code == 422
    assert client.put("/api/admin/arretes/a1", json={"geojson": {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "GeometryCollection", "geometries": []}}]}},
                      headers=h).status_code == 422


def test_login_rate_limited(client):
    h = {"X-Requested-With": "carteat"}
    codes = [client.post("/api/admin/login", json={"password": "faux"}, headers=h).status_code for _ in range(6)]
    assert codes[-1] == 429


def test_password_hash_roundtrip():
    hsh = hash_password("un mot de passe solide")
    assert verify_password("un mot de passe solide", hsh)
    assert not verify_password("autre", hsh)


def test_head_sur_api_publique(client):
    assert client.head("/api/arretes").status_code == 200
    assert client.head("/healthz").status_code == 200


def test_statut_seul_fige_l_arrete(client):
    h = {"X-Requested-With": "carteat"}
    client.post("/api/admin/login", json={"password": "motdepasse-test-123"}, headers=h)
    r = client.put("/api/admin/arretes/a1", json={"statut": "masque"}, headers=h)
    assert r.json()["modifie_manuellement"] is True
    assert [a["id"] for a in client.get("/api/arretes").json()] == []
