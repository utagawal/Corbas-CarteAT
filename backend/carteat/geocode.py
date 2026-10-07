"""Transformation des localisations extraites en géométries (GeoJSON WGS84).

- Adresses (« 51 Rue Centrale ») : API Adresse (Base Adresse Nationale), puis accrochage à la voie OSM.
- Carrefours : intersection des géométries OSM des deux voies.
- Tronçons : portion de la voie entre deux ancres (adresses ou carrefours).
- Voies entières : géométrie OSM découpée aux limites de la commune.
"""

from __future__ import annotations

import difflib
import logging
import time
from typing import Callable, Protocol

import httpx
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .streets import StreetIndex
from .textutil import norm

log = logging.getLogger(__name__)

QUALITY_ORDER = ["none", "approx", "street", "precise"]


class Cache(Protocol):
    def get(self, key: str) -> dict | None: ...
    def set(self, key: str, value: dict) -> None: ...


class BanClient:
    def __init__(self, base_url: str, citycode: str, postcode: str, cache: Cache | None = None,
                 user_agent: str = "CarteAT", timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.citycode = citycode
        self.postcode = postcode
        self.cache = cache
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": user_agent})
        self.failures = 0  # erreurs réseau rencontrées (le géocodage sera retenté plus tard)

    def address(self, num: str, street: str) -> tuple[float, float] | None:
        """Coordonnées d'un numéro ; à défaut, du numéro voisin le plus proche du même côté (±2, ±4, ±6)."""
        try:
            n = int(num)
        except ValueError:
            return None
        for cand in (n, n - 2, n + 2, n - 4, n + 4, n - 6, n + 6):
            if cand <= 0:
                continue
            res = self._lookup(cand, street)
            if res == "erreur":
                return None
            if res:
                return res
        return None

    def _lookup(self, num: int, street: str):
        q = f"{num} {street}"
        key = f"ban2:{self.citycode}:{norm(q)}"
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return tuple(hit["lonlat"]) if hit.get("lonlat") else None
        params = {"q": q, "citycode": self.citycode, "type": "housenumber", "limit": 1}
        for attempt in range(3):
            try:
                r = self.client.get(f"{self.base_url}/search/", params=params)
                r.raise_for_status()
                feats = r.json().get("features", [])
                break
            except Exception as e:  # noqa: BLE001 — l'absence de géocodage n'est pas bloquante
                if attempt == 2:
                    log.warning("API Adresse indisponible pour %r : %s", q, e)
                    self.failures += 1
                    return "erreur"  # pas de mise en cache d'une erreur réseau
                time.sleep(1.5 * (attempt + 1))
        lonlat = None
        if feats:
            p = feats[0]["properties"]
            same_street = difflib.SequenceMatcher(None, norm(p.get("street", "")), norm(street)).ratio() >= 0.75
            if p.get("score", 0) >= 0.5 and same_street and str(p.get("housenumber", "")).startswith(str(num)):
                lonlat = tuple(feats[0]["geometry"]["coordinates"])
        if self.cache is not None:
            self.cache.set(key, {"lonlat": list(lonlat) if lonlat else None})
        return lonlat


class Geocoder:
    def __init__(self, idx: StreetIndex, ban: BanClient | None):
        self.idx = idx
        self.ban = ban

    # ------------------------------------------------------------------ ancres
    def _anchor(self, spec: dict) -> tuple[Point | None, str]:
        """Point (projection locale) d'une ancre et qualité obtenue."""
        if spec["type"] == "intersection":
            a, b = spec["streets"]
            p = self.idx.intersection(a, b)
            return (p, "precise") if p is not None else (None, "none")
        street = spec["street"]
        if self.ban:
            nums = [spec["num"]] + ([spec["num2"]] if spec.get("num2") else [])
            pts = []
            for n in nums:
                ll = self.ban.address(n, street)
                if ll:
                    pts.append(self.idx.from_wgs84(Point(ll)))
            if pts:
                p = Point(sum(q.x for q in pts) / len(pts), sum(q.y for q in pts) / len(pts))
                return p, "precise"
        return None, "none"

    # ------------------------------------------------------------------ localisations
    def locate(self, loc: dict) -> tuple[list[BaseGeometry], str]:
        kind = loc["kind"]
        idx = self.idx
        if kind == "street":
            if loc["street"] in idx.geoms:
                return [idx.street_in_commune(loc["street"])], "street"
            return [], "none"
        if kind == "place":
            if loc["name"] in idx.lieux:
                return [idx.lieux[loc["name"]]], "precise"
            for key, name, geom in idx.places:
                if name == loc["name"]:
                    return [geom], "precise"
            return [], "none"
        if kind == "point":
            spec = loc["anchor"]
            p, q = self._anchor(spec)
            street = spec["street"] if spec["type"] == "address" else spec["streets"][0]
            if p is None:
                if spec["type"] == "address" and street in idx.geoms:
                    return [idx.street_in_commune(street)], "approx"
                return [], "none"
            if spec["type"] == "intersection":
                segs = [s for s in (idx.segment_around(st, p, 30) for st in spec["streets"]) if s is not None]
                return (segs or [p]), q
            seg = idx.segment_around(street, p, 25)
            return [seg if seg is not None else p], q
        if kind == "segment":
            pa, qa = self._anchor(loc["from"])
            pb, qb = self._anchor(loc["to"])
            street = loc.get("street")
            if pa is not None and pb is not None:
                if street:
                    seg = idx.segment_between(street, pa, pb)
                    if seg is not None:
                        return [seg], "precise"
                else:
                    # Deux voies différentes : on passe par leur carrefour.
                    sa = loc["from"].get("street") or loc["from"]["streets"][0]
                    sb = loc["to"].get("street") or loc["to"]["streets"][0]
                    x = idx.intersection(sa, sb)
                    if x is not None:
                        parts = [idx.segment_between(sa, pa, x), idx.segment_between(sb, x, pb)]
                        parts = [g for g in parts if g is not None]
                        if parts:
                            return parts, "precise"
            # Une seule ancre trouvée : petit tronçon autour.
            for p, spec in ((pa, loc["from"]), (pb, loc["to"])):
                if p is not None:
                    st = street or spec.get("street") or spec["streets"][0]
                    seg = idx.segment_around(st, p, 40)
                    if seg is not None:
                        return [seg], "approx"
            if street and street in idx.geoms:
                return [idx.street_in_commune(street)], "approx"
            return [], "none"
        return [], "none"

    def geocode(self, localisations: list[dict], deviation: list[str]) -> dict:
        impact_geoms: list[BaseGeometry] = []
        qualities = []
        for loc in localisations:
            geoms, q = self.locate(loc)
            impact_geoms.extend(geoms)
            qualities.append(q)
        quality = min(qualities, key=QUALITY_ORDER.index) if qualities else "none"
        if impact_geoms and quality == "none":
            quality = "approx"
        features = []
        for g in impact_geoms:
            features.append({"type": "Feature", "properties": {"role": "impact"},
                             "geometry": mapping(self.idx.to_wgs84(g))})
        for name in deviation:
            if name in self.idx.geoms:
                features.append({"type": "Feature", "properties": {"role": "deviation", "name": name},
                                 "geometry": mapping(self.idx.to_wgs84(self.idx.street_in_commune(name)))})
        center = None
        if impact_geoms:
            u = unary_union(impact_geoms)
            c = u.representative_point() if u.geom_type.endswith("Polygon") else (
                u.interpolate(0.5, normalized=True) if u.geom_type == "LineString" else u.centroid)
            # le centroïde d'un ensemble de tronçons peut tomber hors voie : on le recolle au plus proche
            if not u.geom_type.startswith("Point") and u.distance(c) > 1:
                from shapely.ops import nearest_points
                c = nearest_points(u, c)[0]
            cw = self.idx.to_wgs84(c)
            center = [round(cw.x, 6), round(cw.y, 6)]
        return {"geojson": {"type": "FeatureCollection", "features": features},
                "quality": quality, "center": center,
                "incomplete": bool(self.ban and self.ban.failures)}


def geometry_center(fc: dict) -> list[float] | None:
    geoms = [shape(f["geometry"]) for f in fc.get("features", []) if f["properties"].get("role") != "deviation"]
    if not geoms:
        return None
    u = unary_union(geoms)
    c = u.representative_point()
    return [round(c.x, 6), round(c.y, 6)]


GeocodeFn = Callable[[list[dict], list[str]], dict]
