"""Client du registre des actes PubliS²low (API JSON utilisée par le composant web du registre)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


@dataclass
class Acte:
    id: str
    numero: str
    objet: str
    date_decision: str | None
    date_publication: str | None
    pdf_url: str | None
    pdf_name: str | None
    pdf_taille: str | None = None

    def signature(self) -> str:
        """Empreinte des métadonnées publiées : change si l'acte ou son document est modifié au registre.

        On retient l'identifiant du document (dernier segment de l'URL) plutôt que l'URL complète,
        pour qu'un changement d'hôte ou de version de l'API ne fasse pas croire à une modification.
        """
        doc = (self.pdf_url or "").rstrip("/").rsplit("/", 1)[-1]
        objet = " ".join(self.objet.split())
        return "|".join([self.numero, objet, self.date_decision or "", doc, self.pdf_taille or ""])


class Registry:
    def __init__(self, api: str, collectivite_id: str, user_agent: str, timeout: float = 60.0):
        self.base = f"{api.rstrip('/')}/{collectivite_id}/actes"
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True)

    def list_actes(self, search: str = "AT", page_size: int = 100, max_pages: int = 50) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            r = self.client.get(self.base, params={
                "page": page, "limit": page_size, "orderBy[dateDecision]": "DESC", "objetOrNumeroContains": search,
            })
            r.raise_for_status()
            data = r.json()
            out.extend(data.get("actes", []))
            if page * page_size >= data.get("total", 0):
                break
        return out

    def list_at(self, numero_regex: str, start_year: int) -> list[Acte]:
        rx = re.compile(numero_regex, re.I)
        res: dict[str, Acte] = {}
        for a in self.list_actes():
            numero = (a.get("numero") or "").strip()
            if not rx.match(numero):
                continue
            pub = (a.get("datePublication") or a.get("dateDecision") or "")[:10]
            if not pub or int(pub[:4]) < start_year:
                continue
            docs = ([a["documentPrincipal"]] if a.get("documentPrincipal") else []) + (a.get("annexes") or [])
            pdf = next((d for d in docs if d.get("mimeType") == "application/pdf"), None)
            acte_id = (pdf or {}).get("url", "").rstrip("/").split("/")[-2] if pdf else None
            # Identifiant de secours sans « / » ni espace (utilisé dans les URL d'administration).
            acte_id = acte_id or re.sub(r"[^A-Za-z0-9_-]+", "-", f"{numero}-{pub}").strip("-")
            res[acte_id] = Acte(
                id=acte_id,
                numero=re.sub(r"\s+", " ", numero),
                objet=a.get("objet") or "",
                date_decision=(a.get("dateDecision") or "")[:10] or None,
                date_publication=pub or None,
                pdf_url=pdf.get("url") if pdf else None,
                pdf_name=pdf.get("filename") if pdf else None,
                pdf_taille=str(pdf.get("size") or "") if pdf else None,
            )
        return list(res.values())

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        with self.client.stream("GET", url) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        if tmp.read_bytes()[:4] != b"%PDF":
            tmp.unlink(missing_ok=True)
            raise ValueError("Le document téléchargé n'est pas un PDF")
        tmp.replace(dest)
        return dest
