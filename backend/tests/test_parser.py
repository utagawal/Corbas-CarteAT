from datetime import date
from pathlib import Path

import pytest

from carteat import osm
from carteat.dates import extract_calendar
from carteat.parser import anonymize, parse, remove_postal_addresses
from carteat.streets import StreetIndex

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def idx():
    return StreetIndex(osm.load(osm.SEED_PATH), osm.load_lieux())


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_refection_tranchee_242(idx):
    ex = parse(load("at_242_26.txt"), idx)
    assert ex.numero == "242/2026"
    assert ex.categorie == "voirie"
    assert ex.calendrier["debut"] == "2026-10-09"
    assert ex.calendrier["fin"] == "2026-10-30"
    assert {"stationnement", "vitesse", "trottoir"} <= set(ex.impacts)
    assert ex.entreprise == "COIRO TP"
    seg = ex.localisations[0]
    assert seg["kind"] == "segment" and seg["street"] == "Rue Centrale"
    assert seg["to"] == {"type": "intersection", "streets": ["Rue Centrale", "Impasse Jules Pellet"]}


def test_gaz_243(idx):
    ex = parse(load("at_243_26.txt"), idx)
    assert ex.categorie == "reseaux"
    assert (ex.calendrier["debut"], ex.calendrier["fin"]) == ("2026-10-19", "2026-10-30")
    assert ex.localisations == [{"kind": "point", "anchor": {"type": "address", "num": "61", "street": "Rue Marcel Mérieux", "num2": None}}]


def test_demenagement_particulier_anonymise(idx):
    ex = parse(load("at_241_26_anonymise.txt"), idx)
    assert ex.categorie == "demenagement"
    assert ex.particulier is True
    assert ex.entreprise is None
    # deux jours distincts, sans année dans le texte : déduite de la date de signature
    assert ex.calendrier["mode"] == "jours"
    assert ex.calendrier["jours"] == ["2026-10-02", "2026-10-03"]
    assert ex.calendrier["horaires"] == ["17h00–20h00", "8h00–18h00"]
    # l'adresse du demandeur n'est pas confondue avec le lieu de l'intervention
    assert ex.lieu_texte == "51 Rue Centrale"
    assert "DUPONT" not in ex.mesures_texte and "Dupont" not in (ex.objet or "")


def test_tronçon_entre_deux_rues_220(idx):
    ex = parse(load("at_220_26.txt"), idx)
    assert ex.localisations[0]["kind"] == "segment"
    assert ex.lieu_texte == "Chemin de Grange Blanche, entre Avenue du 8 Mai 1945 et Rue Jean Macé"
    assert "circulation_alternee" in ex.impacts


def test_plusieurs_troncons_et_intersections_188(idx):
    ex = parse(load("at_188_26.txt"), idx)
    assert ex.categorie == "evenement"
    assert "route_barree" in ex.impacts
    streets = {l["street"] for l in ex.localisations if l["kind"] == "segment"}
    assert streets == {"Avenue de Corbetta"}
    assert len(ex.localisations) == 2


def test_numeros_entre_le_61(idx):
    ex = parse(load("at_61_26.txt"), idx)
    loc = ex.localisations[0]
    assert loc["kind"] == "segment" and loc["from"]["num"] == "6" and loc["to"]["num"] == "18"


def test_deviation_10(idx):
    ex = parse(load("at_10_26.txt"), idx)
    assert "route_barree" in ex.impacts and "deviation" in ex.impacts
    assert ex.deviation == ["Avenue de Corbetta", "Chemin des Terreaux"]


def test_route_barree_horaires_110(idx):
    ex = parse(load("at_110_26.txt"), idx)
    assert "route_barree" in ex.impacts
    assert ex.localisations == [{"kind": "street", "street": "Rue Centrale"}]
    assert "hors week-end" in ex.calendrier["horaires"]


def test_carrefour_179(idx):
    ex = parse(load("at_179_26.txt"), idx)
    assert ex.localisations[0]["kind"] == "point"
    assert ex.localisations[0]["anchor"]["type"] == "intersection"


def test_annee_deduite_et_plage():
    cal = extract_calendar("Du jeudi 7 mai à 14h au dimanche 10 mai, 10 places réservées", date(2026, 5, 1))
    assert (cal.debut, cal.fin, cal.mode) == (date(2026, 5, 7), date(2026, 5, 10), "plage")
    cal = extract_calendar("le lundi 4 janvier", date(2026, 12, 20))
    assert cal.debut == date(2027, 1, 4)


def test_duree_estimee():
    cal = extract_calendar("À compter du lundi 14 septembre 2026, pour toute la durée des travaux estimée à 1 mois", date(2026, 9, 10))
    assert cal.fin == date(2026, 10, 14)


def test_anonymisation():
    assert "Dupont" not in anonymize("Déménagement de Monsieur Jean Dupont rue Centrale")
    s = remove_postal_addresses("l'entreprise EJC logistic, 24 rue Louis Blanc; 75010 Paris est autorisée au droit du 4 Rue Bernard Buffet")
    assert "Louis Blanc" not in s and "4 Rue Bernard Buffet" in s
    s = remove_postal_addresses("au droit du 53 Avenue de la Villerme - 69960 CORBAS, du vendredi")
    assert "53 Avenue de la Villerme" in s


def test_lieu_dit_local(idx):
    txt = ("Arrêté temporaire N°: 67/2026\nObjet : Stationnements réservés\n\nArticle 1 : Le mercredi 20 mai 2026, 22 places "
           "de stationnement seront réservées sur la première rangée, parking des ombrières au Parc de Loisirs, de 9h30 à 13h.\n")
    ex = parse(txt, idx, date(2026, 5, 1))
    assert ex.localisations == [{"kind": "place", "name": "Parc de Loisirs"}]


def test_entre_carrefour_et_numero(idx):
    txt = ("Article 1 : Le samedi 25 avril 2026, la circulation sera interdite Route de Marennes, dans les deux sens, "
           "entre le chemin des Bruyères et le 300 Route de Marennes.\n")
    ex = parse(txt, idx, date(2026, 4, 1))
    loc = ex.localisations[0]
    assert loc["kind"] == "segment" and loc["street"] == "Route de Marennes"
    assert loc["from"]["type"] == "intersection" and loc["to"] == {"type": "address", "num": "300", "street": "Route de Marennes"}
    # le texte « Lieu » produit est ré-exploitable tel quel par la localisation de l'administration
    from carteat.parser import extract_locations
    assert extract_locations(ex.lieu_texte, idx)[0] == ex.localisations
