"""Development-only SENEX Mega Research Fusion V2 read-only terminal."""
from fastapi import FastAPI
from .api import MegaResearchService, build_router
service = MegaResearchService()
app = FastAPI(title="SENEX MEGA RESEARCH FUSION V2", docs_url=None, redoc_url=None)
app.include_router(build_router(service))
@app.get("/livez")
def livez():
    return {"ok":True,"paper_only":True,"orders_enabled":False,"live_capital_locked":True,"mode":"ISOLATED_DEV_PREDEPLOY"}
