"""Reject HTTP requests from IPs not in API_ALLOWED_IPS."""

from __future__ import annotations

import logging
from typing import FrozenSet, Optional, Set

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("easybop.api")


def parse_allowed_ips(raw: str) -> Optional[FrozenSet[str]]:
    """Return None when allowlist is disabled (* or empty after trim)."""
    value = (raw or "").strip()
    if not value or value == "*":
        return None
    ips: Set[str] = set()
    for part in value.split(","):
        ip = part.strip()
        if ip:
            ips.add(ip)
    return frozenset(ips) if ips else None


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_ips: Optional[FrozenSet[str]]):
        super().__init__(app)
        self.allowed_ips = allowed_ips

    async def dispatch(self, request: Request, call_next):
        if self.allowed_ips is None:
            return await call_next(request)

        client = request.client
        if client is None:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)

        host = client.host
        if host in self.allowed_ips:
            return await call_next(request)

        log.warning("blocked request from %s %s %s", host, request.method, request.url.path)
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
