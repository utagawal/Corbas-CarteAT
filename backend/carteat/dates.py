"""Extraction du calendrier d'intervention dans le texte d'un arrêté."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .textutil import one_line, strip_accents

MONTHS = {
    "janvier": 1, "janv": 1, "fevrier": 2, "fevr": 2, "fev": 2, "mars": 3, "avril": 4, "avr": 4,
    "mai": 5, "juin": 6, "juillet": 7, "juil": 7, "aout": 8, "septembre": 9, "sept": 9,
    "octobre": 10, "oct": 10, "novembre": 11, "nov": 11, "decembre": 12, "dec": 12,
}
DAYS = r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"
_MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))

# « 9 octobre 2026 », « 1er septembre », « vendredi 2 octobre »
RX_TEXT_DATE = re.compile(
    rf"(?:\b{DAYS}\s+)?\b(\d{{1,2}})\s*(?:er)?\s+({_MONTH_RX})\.?(?:\s+(\d{{4}}))?\b",
    re.I,
)
# « 09/10/26 », « 28/09 », « 06/01/2026 »
RX_NUM_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/])")

RX_HOURS = re.compile(
    r"(?:de|entre)\s+(\d{1,2})\s*h\s*(\d{2})?\s*(?:à|a|et|jusqu'à)\s+(\d{1,2})\s*h\s*(\d{2})?", re.I
)
RX_FROM_HOUR = re.compile(r"à partir de\s+(\d{1,2})\s*(?:h|heures)\s*(\d{2})?", re.I)
RX_DURATION = re.compile(
    r"dur[ée]e[^.;]{0,40}?estim[ée]e?\s+à\s+(\d+|un|une|deux|trois|quatre)\s+(jours?|semaines?|mois)", re.I
)
_WORD_NUM = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4}


@dataclass
class FoundDate:
    d: date
    start: int
    end: int
    explicit_year: bool


@dataclass
class Calendar:
    debut: date | None = None
    fin: date | None = None
    jours: list[date] = field(default_factory=list)  # dates ponctuelles (mode « jours »)
    horaires: list[str] = field(default_factory=list)
    mode: str = "plage"  # « plage » (du … au …) ou « jours » (le X et le Y)
    remarques: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "debut": self.debut.isoformat() if self.debut else None,
            "fin": self.fin.isoformat() if self.fin else None,
            "jours": [d.isoformat() for d in self.jours],
            "horaires": self.horaires,
            "mode": self.mode,
            "remarques": self.remarques,
        }


def _infer_year(day: int, month: int, ref: date) -> date | None:
    try:
        d = date(ref.year, month, day)
    except ValueError:
        return None
    # Un arrêté porte sur l'avenir proche : une date « passée » de plus de 4 mois est l'an prochain.
    if d < ref - timedelta(days=120):
        try:
            d = date(ref.year + 1, month, day)
        except ValueError:
            return None
    return d


def find_dates(text: str, ref: date) -> list[FoundDate]:
    found: list[FoundDate] = []
    plain = strip_accents(text)
    for m in RX_TEXT_DATE.finditer(plain):
        day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
        if not 1 <= day <= 31:
            continue
        if m.group(3):
            year = int(m.group(3))
            if not 2000 <= year <= 2100:
                continue
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            found.append(FoundDate(d, m.start(), m.end(), True))
        else:
            d = _infer_year(day, month, ref)
            if d:
                found.append(FoundDate(d, m.start(), m.end(), False))
    for m in RX_NUM_DATE.finditer(plain):
        day, month = int(m.group(1)), int(m.group(2))
        if not (1 <= day <= 31 and 1 <= month <= 12):
            continue
        if any(f.start <= m.start() < f.end for f in found):
            continue
        y = m.group(3)
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            try:
                found.append(FoundDate(date(year, month, day), m.start(), m.end(), True))
            except ValueError:
                pass
        else:
            d = _infer_year(day, month, ref)
            if d:
                found.append(FoundDate(d, m.start(), m.end(), False))
    found.sort(key=lambda f: f.start)
    # Ne garder que des dates plausibles pour un arrêté signé à la date de référence.
    return [f for f in found if ref - timedelta(days=400) <= f.d <= ref + timedelta(days=365 * 8)]


def _fmt_h(h: str, m: str | None) -> str:
    return f"{int(h)}h{m or '00'}"


def find_hours(text: str) -> list[str]:
    out: list[str] = []
    for m in RX_HOURS.finditer(text):
        h = f"{_fmt_h(m.group(1), m.group(2))}–{_fmt_h(m.group(3), m.group(4))}"
        if h not in out:
            out.append(h)
    for m in RX_FROM_HOUR.finditer(text):
        h = f"à partir de {_fmt_h(m.group(1), m.group(2))}"
        if h not in out:
            out.append(h)
    low = strip_accents(text.lower())
    if re.search(r"du lundi au vendredi", low):
        out.append("du lundi au vendredi")
    if re.search(r"hors\s+week[ -]?end", low):
        out.append("hors week-end")
    return out


def _add_duration(start: date, qty: int, unit: str) -> date:
    unit = unit.lower()
    if unit.startswith("jour"):
        return start + timedelta(days=qty - 1)
    if unit.startswith("semaine"):
        return start + timedelta(days=7 * qty - 1)
    month = start.month - 1 + qty
    year = start.year + month // 12
    month = month % 12 + 1
    day = min(start.day, 28)
    return date(year, month, day)


RX_RANGE_LINK = re.compile(
    r"^[\s,]*(?:(?:a|à|de|des)?\s*\d{1,2}\s*(?:h|heures)\s*\d{0,2}[\s,]*)?(?:et\s+)?(?:au|jusqu)\b", re.I
)


def _is_range(text: str, dates: list[FoundDate]) -> bool:
    """Vrai si deux dates consécutives sont reliées par « au » / « jusqu'au » (plage continue)."""
    plain = strip_accents(text)
    for a, b in zip(dates, dates[1:]):
        between = plain[a.end:b.start]
        if RX_RANGE_LINK.match(between):
            return True
        if len(between) < 50 and re.search(r"\b(au|jusqu)", between) and not re.search(r"\bet le\b", between):
            return True
    return False


def extract_calendar(text: str, ref: date) -> Calendar:
    """`text` : texte des articles réglementaires (sans les visas ni la date de signature)."""
    text = one_line(text)
    cal = Calendar()
    dates = find_dates(text, ref)
    cal.horaires = find_hours(text)
    if not dates:
        return cal
    ds = sorted({f.d for f in dates})
    cal.debut, cal.fin = ds[0], ds[-1]
    if len(ds) == 1:
        m = RX_DURATION.search(text)
        if m:
            qty = int(m.group(1)) if m.group(1).isdigit() else _WORD_NUM[m.group(1).lower()]
            cal.fin = _add_duration(cal.debut, qty, m.group(2))
            cal.remarques.append(f"durée estimée à {m.group(1)} {m.group(2)}")
    is_range = _is_range(text, dates)
    if not is_range and len(ds) > 1:
        # « le vendredi 2 octobre de 17h à 20h et le samedi 3 octobre de 8h à 18h »
        cal.mode = "jours"
        cal.jours = ds
    elif len(ds) == 1 and cal.fin == cal.debut:
        cal.mode = "jours"
        cal.jours = ds
    if re.search(r"fin d(u|es) (défilé|travaux|manifestation)", text, re.I) and not cal.remarques:
        cal.remarques.append("jusqu'à la fin " + re.search(r"fin d(?:u|es) (\w+)", text, re.I).group(0)[4:])
    return cal
