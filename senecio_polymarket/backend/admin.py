"""ORDER-070 private control app. Never mounted by the public runtime."""
from __future__ import annotations

import hmac
import os
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from . import main as legacy



def _admin_token_status(authorization: str | None) -> tuple[bool, int, str]:
    expected = (os.environ.get("SENEX_ADMIN_TOKEN") or "").strip()
    if not expected:
        return False, 503, "ADMIN_AUTH_NOT_CONFIGURED"
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        return False, 401, "ADMIN_AUTH_REQUIRED"
    if not hmac.compare_digest(authorization[len(prefix):], expected):
        return False, 403, "ADMIN_AUTH_INVALID"
    return True, 200, "ADMIN_AUTH_OK"

def require_admin_auth(authorization: str | None = Header(default=None)) -> None:
    ok, status, detail = _admin_token_status(authorization)
    if not ok:
        raise HTTPException(status_code=status, detail=detail)


admin_app = FastAPI(
    title="SENEX ADMIN CONTROL",
    version="ORDER-070-R1",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    dependencies=[Depends(require_admin_auth)],
)

@admin_app.middleware("http")
async def admin_auth_guard(request: Request, call_next):
    ok, status, detail = _admin_token_status(request.headers.get("authorization"))
    if not ok:
        return JSONResponse({"detail": detail}, status_code=status)
    return await call_next(request)


SAFE = {"GET", "HEAD", "OPTIONS"}
for route in legacy.app.router.routes:
    if isinstance(route, APIRoute) and set(route.methods or set()) - SAFE:
        admin_app.router.routes.append(route)
