"""Extraction du texte d'un PDF : couche texte native si présente, sinon OCR Tesseract."""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf
import pytesseract
from PIL import Image

log = logging.getLogger(__name__)

MIN_NATIVE_CHARS = 80  # en deçà, la page est considérée comme scannée


def pdf_to_text(path: Path, lang: str = "fra", dpi: int = 300, max_pages: int = 12) -> tuple[str, str]:
    """Retourne (texte, méthode) où méthode ∈ {"natif", "ocr", "mixte"}."""
    doc = pymupdf.open(path)
    pages, modes = [], set()
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            txt = page.get_text()
            if len(txt.strip()) >= MIN_NATIVE_CHARS:
                modes.add("natif")
            else:
                pix = page.get_pixmap(dpi=dpi)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                txt = pytesseract.image_to_string(img, lang=lang, config="--psm 3")
                modes.add("ocr")
            pages.append(txt)
    finally:
        doc.close()
    mode = modes.pop() if len(modes) == 1 else ("mixte" if modes else "vide")
    return "\n\f\n".join(pages), mode
