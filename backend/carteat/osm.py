"""Récupération et mise en cache des données OpenStreetMap de la commune.

Le format interne (« référentiel ») est volontairement compact :

    {
      "generated": "2026-10-07T06:00:00Z",
      "source": "overpass" | "osm-api",
      "boundary": <GeoJSON (Multi)Polygon de la commune>,
      "ways":   [{"id": 1, "name": "Rue Centrale", "highway": "secondary", "coords": [[lon, lat], ...]}],
      "places": [{"id": "w2", "name": "Place du Costel", "kind": "place", "geometry": <GeoJSON>}]
    }

Données © contributeurs OpenStreetMap, licence ODbL.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx
from shapely.geometry import LineString, MultiPolygon, Polygon, mapping
from shapely.ops import linemerge, polygonize, unary_union

log = logging.getLogger(__name__)

SEED_PATH = Path(__file__).parent / "data" / "osm_corbas.json.gz"
LIEUX_PATH = Path(__file__).parent / "data" / "lieux.json"

# Voies routières utiles (on exclut les chemins de service privés sans nom).
PLACE_TAGS = ("place", "amenity", "leisure", "building", "landuse", "highway", "public_transport")


def _overpass_query(insee: str, bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    return f"""
[out:json][timeout:120][bbox:{s},{w},{n},{e}];
(
  way["highway"]["name"];
  way["highway"="pedestrian"];
  way["amenity"="parking"];
  way["place"]["name"];
  way["leisure"]["name"];
  way["amenity"]["name"];
  way["building"]["name"];
  node["name"]["amenity"];
  node["name"]["place"];
  node["name"]["leisure"];
);
out geom;
rel["ref:INSEE"="{insee}"]["boundary"="administrative"];
out geom;
"""


def _kind(tags: dict) -> str:
    for k in PLACE_TAGS:
        if k in tags:
            return k if k != "highway" else "square"
    return "other"


def _ring_polygons(member_lines: list[list[tuple[float, float]]]) -> list[Polygon]:
    lines = [LineString(c) for c in member_lines if len(c) >= 2]
    merged = linemerge(lines)
    return list(polygonize(merged))


def from_overpass(data: dict, insee: str) -> dict:
    ways, places = [], []
    boundary = None
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if el["type"] == "relation" and tags.get("ref:INSEE") == insee:
            outer = [
                [(p["lon"], p["lat"]) for p in m.get("geometry", [])]
                for m in el.get("members", [])
                if m.get("type") == "way" and m.get("role") in ("outer", "")
            ]
            polys = _ring_polygons(outer)
            if polys:
                boundary = mapping(unary_union(polys))
            continue
        if el["type"] == "way":
            coords = [[p["lon"], p["lat"]] for p in el.get("geometry", [])]
            _add_way(ways, places, el["id"], tags, coords)
        elif el["type"] == "node" and tags.get("name"):
            places.append(
                {
                    "id": f"n{el['id']}",
                    "name": tags["name"],
                    "kind": _kind(tags),
                    "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
                }
            )
    return _finalize(ways, places, boundary, "overpass")


def from_osm_xml(xml_files: list[Path], boundary_xml: Path | None, insee: str) -> dict:
    """Construit le référentiel à partir d'extraits de l'API OSM 0.6 (/map, /relation/full)."""
    nodes: dict[str, tuple[float, float]] = {}
    raw_ways: dict[str, tuple[dict, list[str]]] = {}
    tagged_nodes: dict[str, dict] = {}
    for f in xml_files + ([boundary_xml] if boundary_xml else []):
        root = ET.parse(f).getroot()
        for n in root.iter("node"):
            nodes[n.get("id")] = (float(n.get("lon")), float(n.get("lat")))
            tags = {t.get("k"): t.get("v") for t in n.iter("tag")}
            if tags.get("name"):
                tagged_nodes[n.get("id")] = tags
        for w in root.iter("way"):
            tags = {t.get("k"): t.get("v") for t in w.iter("tag")}
            raw_ways[w.get("id")] = (tags, [nd.get("ref") for nd in w.iter("nd")])
    ways, places = [], []
    for wid, (tags, refs) in raw_ways.items():
        coords = [list(nodes[r]) for r in refs if r in nodes]
        keep = (
            ("highway" in tags and (tags.get("name") or tags.get("highway") == "pedestrian"))
            or tags.get("amenity") == "parking"
            or (tags.get("name") and any(k in tags for k in ("place", "leisure", "amenity", "building")))
        )
        if keep:
            _add_way(ways, places, int(wid), tags, coords)
    for nid, tags in tagged_nodes.items():
        if any(k in tags for k in ("amenity", "place", "leisure")):
            places.append(
                {"id": f"n{nid}", "name": tags["name"], "kind": _kind(tags),
                 "geometry": {"type": "Point", "coordinates": list(nodes[nid])}}
            )
    boundary = None
    if boundary_xml:
        root = ET.parse(boundary_xml).getroot()
        for rel in root.iter("relation"):
            tags = {t.get("k"): t.get("v") for t in rel.iter("tag")}
            if tags.get("ref:INSEE") != insee:
                continue
            outer = []
            for m in rel.iter("member"):
                if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("ref") in raw_ways:
                    outer.append([nodes[r] for r in raw_ways[m.get("ref")][1] if r in nodes])
            polys = _ring_polygons(outer)
            if polys:
                boundary = mapping(unary_union(polys))
    return _finalize(ways, places, boundary, "osm-api")


def _add_way(ways: list, places: list, wid: int, tags: dict, coords: list) -> None:
    if len(coords) < 2:
        return
    closed = coords[0] == coords[-1] and len(coords) >= 4
    name = tags.get("name")
    is_area = closed and (
        tags.get("area") == "yes"
        or any(k in tags for k in ("amenity", "place", "leisure", "building"))
        or tags.get("highway") == "pedestrian"
    )
    if "highway" in tags and name and not is_area:
        ways.append({"id": wid, "name": name, "highway": tags["highway"], "coords": coords})
    elif name or tags.get("amenity") == "parking":
        geom = {"type": "Polygon", "coordinates": [coords]} if closed else {"type": "LineString", "coordinates": coords}
        places.append({"id": f"w{wid}", "name": name or "Parking", "kind": _kind(tags), "geometry": geom})


def _finalize(ways, places, boundary, source) -> dict:
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "boundary": boundary,
        "ways": ways,
        "places": places,
    }


def fetch_overpass(endpoints: list[str], insee: str, bbox, user_agent: str) -> dict:
    query = _overpass_query(insee, bbox)
    last_err: Exception | None = None
    for url in endpoints:
        for attempt in range(2):
            try:
                r = httpx.post(url, data={"data": query}, timeout=180, headers={"User-Agent": user_agent})
                r.raise_for_status()
                data = from_overpass(r.json(), insee)
                if len(data["ways"]) < 20:
                    raise ValueError(f"réponse Overpass trop pauvre ({len(data['ways'])} voies)")
                return data
            except Exception as e:  # noqa: BLE001 — on essaie le miroir suivant
                last_err = e
                log.warning("Overpass %s (essai %d) : %s", url, attempt + 1, e)
                time.sleep(5)
    raise RuntimeError(f"Aucun serveur Overpass n'a répondu : {last_err}")


def load(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def boundary_polygon(data: dict):
    from shapely.geometry import shape

    if not data.get("boundary"):
        return None
    g = shape(data["boundary"])
    return g if isinstance(g, (Polygon, MultiPolygon)) else None


def load_lieux(override: Path | None = None) -> list[dict]:
    """Lieux-dits livrés avec l'application, complétés/remplacés par `override` (même « nom »)."""
    by_name = {l["nom"]: l for l in json.loads(LIEUX_PATH.read_text(encoding="utf-8"))["lieux"]}
    if override and override.exists():
        try:
            for l in json.loads(override.read_text(encoding="utf-8")).get("lieux", []):
                by_name[l["nom"]] = l
        except (ValueError, KeyError) as e:
            log.error("Fichier de lieux-dits %s invalide : %s", override, e)
    return list(by_name.values())
