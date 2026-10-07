"""Mot de passe administrateur (scrypt), limitation des tentatives et en-têtes de sécurité."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


def hash_password(password: str, n: int = 2**14, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${n}${r}${p}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored_hash: str = "", plain: str = "") -> bool:
    if stored_hash:
        try:
            _, n, r, p, salt, dk = stored_hash.split("$")
            calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                  dklen=len(base64.b64decode(dk)))
            return hmac.compare_digest(calc, base64.b64decode(dk))
        except (ValueError, TypeError):
            return False
    if plain:
        return hmac.compare_digest(password.encode(), plain.encode())
    return False


class RateLimiter:
    """5 tentatives par tranche de 15 minutes et par adresse IP."""

    def __init__(self, max_attempts: int = 5, window: int = 900):
        self.max, self.window = max_attempts, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            return False
        q.append(now)
        return True


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


class SecurityHeaders(BaseHTTPMiddleware):
    def __init__(self, app, map_style_url: str, frame_ancestors: str):
        super().__init__(app)
        tiles = urlparse(map_style_url)
        tiles_origin = f"{tiles.scheme}://{tiles.netloc}" if tiles.netloc else ""
        self.csp = "; ".join([
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",  # MapLibre positionne ses éléments par attribut style
            f"img-src 'self' data: blob: {tiles_origin}",
            "font-src 'self' data:",
            f"connect-src 'self' {tiles_origin}",
            "worker-src 'self' blob:",
            "child-src blob:",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            f"frame-ancestors {frame_ancestors}",
        ])

    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", self.csp)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
        if request.url.path.startswith("/api/admin") or request.url.path.endswith("admin.html"):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp
