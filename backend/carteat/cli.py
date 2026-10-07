"""Ligne de commande : `python -m carteat.cli <commande>`."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from sqlalchemy import select

from .config import get_settings
from .db import Arrete, Database
from .pipeline import Pipeline
from .security import hash_password


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="carteat")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="Lance le serveur web")
    sub.add_parser("sync", help="Synchronise les arrêtés depuis le registre")
    rp = sub.add_parser("reprocess", help="Ré-extrait et re-géocode tous les arrêtés non modifiés à la main")
    rp.add_argument("--ocr", action="store_true", help="Refaire aussi l'OCR")
    sub.add_parser("refresh-osm", help="Met à jour le référentiel des voies (Overpass)")
    sub.add_parser("hash-password", help="Génère l'empreinte du mot de passe administrateur")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    s = get_settings()
    if args.cmd == "hash-password":
        pwd = getpass.getpass("Mot de passe administrateur : ")
        if len(pwd) < 12:
            print("Choisissez au moins 12 caractères.", file=sys.stderr)
            return 1
        if pwd != getpass.getpass("Confirmation : "):
            print("Les mots de passe ne correspondent pas.", file=sys.stderr)
            return 1
        # « $$ » : échappement nécessaire dans un fichier .env lu par docker compose
        print("ADMIN_PASSWORD_HASH=" + hash_password(pwd).replace("$", "$$"))
        return 0
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("carteat.main:app", host="0.0.0.0", port=s.port, proxy_headers=True,
                    forwarded_allow_ips="*", log_level="info")
        return 0

    db = Database(s.db_path)
    p = Pipeline(s, db)
    if args.cmd == "sync":
        print(p.sync("cli"))
    elif args.cmd == "refresh-osm":
        print(p.ensure_osm(force=True))
    elif args.cmd == "reprocess":
        with db.session() as ses:
            ids = list(ses.scalars(select(Arrete.id).where(Arrete.modifie_manuellement.is_(False))))
        for i, aid in enumerate(ids, 1):
            p.reprocess(aid, ocr=args.ocr)
            print(f"{i}/{len(ids)}", end="\r")
        print(f"{len(ids)} arrêtés retraités")
    return 0


if __name__ == "__main__":
    sys.exit(main())
