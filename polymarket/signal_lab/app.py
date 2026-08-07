"""Development entry point for SENEX Live Terminal V1.

Production/runtime integration is intentionally deferred while lane 017 is
frozen.  This app is a read-only development surface and can later be mounted
inside the existing FastAPI process without requiring another paid service.
"""
from fastapi import FastAPI

from .api import SignalLabService, build_router

service = SignalLabService()
app = FastAPI(title="SENEX LIVE TERMINAL V1", docs_url=None, redoc_url=None)
app.include_router(build_router(service))


@app.get("/livez")
def livez():
    return {"ok": True, "paper_only": True, "orders_enabled": False, "live_capital_locked": True}
