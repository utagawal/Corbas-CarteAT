"""Petits utilitaires de normalisation de texte (français, OCR)."""

from __future__ import annotations

import re
import unicodedata

# Corrections d'artefacts OCR fréquents.
_OCR_FIXES = [
    (re.compile(r"(\d)\s*[”\"'’]\s+(?=[a-zéû])"), r"\1er "),  # « 1” septembre » -> « 1er septembre »
    (re.compile(r"\b1\s*er\b"), "1er"),
    (re.compile(r"[—–]"), "-"),
    (re.compile(r"[«»“”]"), '"'),
    (re.compile(r"[’`]"), "'"),
    (re.compile(r"-\n(?=[a-zé])"), ""),  # césure en fin de ligne
    (re.compile(r" | "), " "),
]


def clean_ocr(text: str) -> str:
    for rx, rep in _OCR_FIXES:
        text = rx.sub(rep, text)
    return text


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s: str) -> str:
    """Minuscule, sans accents, ponctuation → espaces, espaces compactés."""
    s = strip_accents(s.lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()
