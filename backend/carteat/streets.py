"""Index des voies de la commune (OSM) : recherche de noms dans un texte et calculs géométriques.

Les calculs se font dans une projection locale équirectangulaire en mètres, largement
suffisante à l'échelle d'une commune.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass

from shapely.geometry import LineString, MultiLineString, Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, nearest_points, substring, transform, unary_union

from .textutil import norm

STREET_TYPES = {
    "rue": "rue", "r": "rue", "avenue": "avenue", "av": "avenue", "ave": "avenue",
    "chemin": "chemin", "ch": "chemin", "che": "chemin", "route": "route", "rte": "route",
    "impasse": "impasse", "imp": "impasse", "allee": "allee", "allees": "allee",
    "place": "place", "pl": "place", "boulevard": "boulevard", "bd": "boulevard",
    "montee": "montee", "square": "square", "parvis": "parvis", "passage": "passage",
    "cours": "cours", "quai": "quai", "promenade": "promenade", "sentier": "sentier",
    "esplanade": "esplanade", "rond": "rond", "lotissement": "lotissement", "clos": "clos",
    "residence": "residence", "voie": "voie",
}


def _num_norm(s: str) -> str:
    """Normalise et supprime les zéros initiaux (« 08 mai 45 » ~ « 8 mai 45 »)."""
    return re.sub(r"\b0+(\d)", r"\1", norm(s))


def _aliases(key: str) -> set[str]:
    out = {key}
    # « avenue du 8 mai 1945 » ↔ « avenue du 8 mai 45 »
    out |= {re.sub(r"\b(19|20)(\d{2})\b", r"\2", k) for k in list(out)}
    # « rue des freres lumiere » ↔ « rue des freres lumieres »
    out |= {re.sub(r"s\b", "", k) for k in list(out)}
    return out


@dataclass
class StreetMatch:
    name: str  # nom officiel OSM
    start: int  # position (en mots) dans le texte normalisé
    end: int
    score: float


class Projector:
    def __init__(self, lon0: float, lat0: float):
        self.lon0, self.lat0 = lon0, lat0
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 110540.0

    def fwd(self, g: BaseGeometry) -> BaseGeometry:
        return transform(lambda x, y, z=None: ((x - self.lon0) * self.kx, (y - self.lat0) * self.ky), g)

    def inv(self, g: BaseGeometry) -> BaseGeometry:
        return transform(lambda x, y, z=None: (x / self.kx + self.lon0, y / self.ky + self.lat0), g)


class StreetIndex:
    def __init__(self, data: dict):
        self.data = data
        b = shape(data["boundary"]) if data.get("boundary") else None
        if b is not None:
            c = b.centroid
            self.proj = Projector(c.x, c.y)
        else:
            xs = [p[0] for w in data["ways"] for p in w["coords"]]
            ys = [p[1] for w in data["ways"] for p in w["coords"]]
            self.proj = Projector(sum(xs) / len(xs), sum(ys) / len(ys))
        self.boundary = self.proj.fwd(b) if b is not None else None
        by_name: dict[str, list[LineString]] = {}
        for w in data["ways"]:
            by_name.setdefault(w["name"], []).append(LineString(w["coords"]))
        self.geoms: dict[str, BaseGeometry] = {}
        for name, lines in by_name.items():
            u = unary_union([self.proj.fwd(l) for l in lines])
            self.geoms[name] = linemerge(u) if isinstance(u, MultiLineString) else u
        # Clés normalisées → nom officiel
        self.keys: dict[str, str] = {}
        for name in self.geoms:
            for k in _aliases(" ".join(self._canon_type(_num_norm(name).split()))):
                # en cas de doublon (voie homonyme hors commune), on garde la plus longue
                if k not in self.keys or self.geoms[name].length > self.geoms[self.keys[k]].length:
                    self.keys[k] = name
        # Alias courts « type + dernier mot » (« rue mirabeau » → Rue Comte de Mirabeau), si non ambigus.
        short: dict[str, set[str]] = {}
        for name in self.geoms:
            t = _num_norm(name).split()
            if len(t) >= 3 and t[0] in STREET_TYPES and not t[-1].isdigit():
                short.setdefault(f"{STREET_TYPES[t[0]]} {t[-1]}", set()).add(name)
        for k, names in short.items():
            if len(names) == 1 and k not in self.keys:
                self.keys[k] = next(iter(names))
        self.key_tokens = {k: k.split() for k in self.keys}
        self.max_len = max(len(t) for t in self.key_tokens.values())
        self.places = []
        for p in data.get("places", []):
            if p.get("name"):
                self.places.append((_num_norm(p["name"]), p["name"], self.proj.fwd(shape(p["geometry"]))))

    # ------------------------------------------------------------------ recherche de noms
    def find_streets(self, tokens: list[str]) -> list[StreetMatch]:
        """Repère les noms de voies dans une liste de mots normalisés (sans chevauchement)."""
        matches: list[StreetMatch] = []
        i = 0
        while i < len(tokens):
            if tokens[i] not in STREET_TYPES:
                i += 1
                continue
            best: StreetMatch | None = None
            # 1) correspondance exacte, la plus longue d'abord
            for n in range(min(self.max_len + 1, len(tokens) - i), 1, -1):
                cand = " ".join(self._canon_type(tokens[i:i + n]))
                cand = re.sub(r"\b0+(\d)", r"\1", cand)
                for c in (cand, re.sub(r"s\b", "", cand)):
                    if c in self.keys:
                        best = StreetMatch(self.keys[c], i, i + n, 1.0)
                        break
                if best:
                    break
            # 2) correspondance approchée (erreurs d'OCR, coquilles)
            if not best:
                for n in range(min(self.max_len + 1, len(tokens) - i), 1, -1):
                    cand = " ".join(self._canon_type(tokens[i:i + n]))
                    for k, name in self.keys.items():
                        kt = self.key_tokens[k]
                        if kt[0] != self._canon_type([tokens[i]])[0] or abs(len(kt) - n) > 1:
                            continue
                        r = difflib.SequenceMatcher(None, cand, k).ratio()
                        if r >= 0.88 and (not best or r > best.score):
                            best = StreetMatch(name, i, i + n, r)
                    if best:
                        break
            if best:
                matches.append(best)
                i = best.end
            else:
                i += 1
        return matches

    @staticmethod
    def _canon_type(toks: list[str]) -> list[str]:
        if toks and toks[0] in STREET_TYPES:
            return [STREET_TYPES[toks[0]]] + toks[1:]
        return toks

    def find_places(self, text_norm: str) -> list[tuple[str, BaseGeometry]]:
        out = []
        for key, name, geom in self.places:
            if len(key) >= 6 and re.search(rf"\b{re.escape(key)}\b", text_norm):
                out.append((name, geom))
        return out

    # ------------------------------------------------------------------ géométrie
    def geom(self, name: str) -> BaseGeometry:
        return self.geoms[name]

    def street_in_commune(self, name: str) -> BaseGeometry:
        g = self.geoms[name]
        if self.boundary is not None:
            clipped = g.intersection(self.boundary.buffer(30))
            if not clipped.is_empty:
                return clipped
        return g

    def intersection(self, a: str, b: str, tol: float = 40.0) -> Point | None:
        ga, gb = self.geoms.get(a), self.geoms.get(b)
        if ga is None or gb is None:
            return None
        inter = ga.intersection(gb)
        if not inter.is_empty:
            return inter.centroid if inter.geom_type != "Point" else inter
        pa, pb = nearest_points(ga, gb)
        if pa.distance(pb) <= tol:
            return Point((pa.x + pb.x) / 2, (pa.y + pb.y) / 2)
        return None

    def _parts(self, name: str) -> list[LineString]:
        g = self.geoms[name]
        if isinstance(g, LineString):
            return [g]
        return [p for p in getattr(g, "geoms", []) if isinstance(p, LineString)]

    def segment_between(self, name: str, p1: Point, p2: Point) -> BaseGeometry | None:
        """Portion de la voie `name` entre deux points (projetés sur la voie)."""
        best = None
        for part in self._parts(name):
            d = part.distance(p1) + part.distance(p2)
            if best is None or d < best[0]:
                best = (d, part)
        if best is None or best[0] > 120:
            return None
        part = best[1]
        a, b = part.project(p1), part.project(p2)
        if a > b:
            a, b = b, a
        if b - a < 10:
            return self.segment_around(name, p1)
        return substring(part, a, b)

    def segment_around(self, name: str, p: Point, half: float = 25.0) -> BaseGeometry | None:
        best = None
        for part in self._parts(name):
            d = part.distance(p)
            if best is None or d < best[0]:
                best = (d, part)
        if best is None or best[0] > 150:
            return None
        part = best[1]
        x = part.project(p)
        return substring(part, max(0.0, x - half), min(part.length, x + half))

    def street_point(self, name: str) -> Point:
        g = self.street_in_commune(name)
        return g.interpolate(0.5, normalized=True) if hasattr(g, "interpolate") else g.centroid

    def to_wgs84(self, g: BaseGeometry) -> BaseGeometry:
        return self.proj.inv(g)

    def from_wgs84(self, g: BaseGeometry) -> BaseGeometry:
        return self.proj.fwd(g)


def multiline(geoms: list[BaseGeometry]) -> BaseGeometry:
    lines = []
    for g in geoms:
        if isinstance(g, LineString):
            lines.append(g)
        elif isinstance(g, MultiLineString):
            lines.extend(g.geoms)
    return MultiLineString(lines) if lines else None
