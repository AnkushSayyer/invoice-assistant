"""HTTP Basic Auth gate for the whole app.

Enabled only when both BASIC_AUTH_USERNAME and BASIC_AUTH_PASSWORD are set, so
local development and the test suite run unauthenticated by default. On a public
deployment (e.g. Cloud Run) set both env vars to require a shared login.
"""

import base64
import binascii
import os
import secrets
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

# Paths that must stay reachable without credentials (platform health checks).
EXEMPT_PATHS: frozenset[str] = frozenset({"/health"})


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that lack valid HTTP Basic credentials."""

    def __init__(
        self,
        app: Callable,
        *,
        username: str,
        password: str,
        realm: str = "InvoiceOps AI",
    ) -> None:
        super().__init__(app)
        self._username = username
        self._password = password
        self._realm = realm

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        username, password = _parse_basic_credentials(
            request.headers.get("Authorization")
        )
        # Compare both halves even if the first fails, to avoid leaking which
        # part was wrong via timing.
        user_ok = secrets.compare_digest(username, self._username)
        pass_ok = secrets.compare_digest(password, self._password)
        if user_ok and pass_ok:
            return await call_next(request)

        return PlainTextResponse(
            "Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{self._realm}"'},
        )


def _parse_basic_credentials(header: str | None) -> tuple[str, str]:
    """Return (username, password) from an Authorization header, or ('', '')."""
    if not header:
        return "", ""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return "", ""
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return "", ""
    username, _, password = decoded.partition(":")
    return username, password


def configure_basic_auth(app) -> bool:
    """Attach the Basic Auth middleware if credentials are configured.

    Returns True when auth is enabled, False when left open (dev/test).
    """
    username = os.getenv("BASIC_AUTH_USERNAME", "").strip()
    password = os.getenv("BASIC_AUTH_PASSWORD", "")
    if not username or not password:
        return False
    app.add_middleware(
        BasicAuthMiddleware, username=username, password=password
    )
    return True
