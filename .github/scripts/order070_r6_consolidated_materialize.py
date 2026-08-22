from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()
EXPECTED_HEAD = "b438c0d6dc156d4183929366963df988d97a5283"
EXPECTED_TREE = "e1e4b002aa90402517edb90412b755bc8529327e"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    src = read(path)
    count = src.count(old)
    if count != 1:
        raise RuntimeError(f"ANCHOR_DRIFT:{path}:{old[:80]!r}:count={count}")
    write(path, src.replace(old, new, 1))


# F1 + F2: explicit positive public surface; public authority reads are observational only.
main_path = "senecio_polymarket/backend/main_real.py"
replace_once(main_path, "import os\nfrom contextlib", "import os\nfrom pathlib import Path\nfrom contextlib")
replace_once(
    main_path,
    "from fastapi import FastAPI, Query, Request\nfrom fastapi.responses import JSONResponse\nfrom fastapi.routing import APIRoute\nfrom starlette.routing import Mount, WebSocketRoute",
    "from fastapi import FastAPI, HTTPException, Query, Request\nfrom fastapi.responses import FileResponse, JSONResponse\nfrom fastapi.staticfiles import StaticFiles",
)
old_boundary = '''SAFE_PUBLIC_METHODS = {"GET", "HEAD", "OPTIONS"}\nOVERRIDDEN_GET_PATHS = {\n    "/api/health",\n    "/api/oracle/state",\n    "/api/oracle/score",\n    "/api/portfolio/live_gate",\n}\n\n# R6: production public routing is fail-closed for optional heavy analytics.\n# These legacy GET handlers perform lazy imports/initialization of research or\n# anti-fragility stacks. They remain available in the legacy/admin application,\n# but are never mounted by the unauthenticated production public app.\nOPTIONAL_HEAVY_PUBLIC_DENY_PREFIXES = (\n    "/api/research",\n    "/api/antifragility",\n)\nOPTIONAL_HEAVY_PUBLIC_DENY_PATHS = {\n    "/api/observability",\n    "/metrics",\n}\n\n\ndef _legacy_public_route_allowed(path: str) -> bool:\n    """Default-deny legacy routes that can allocate optional heavy subsystems."""\n    if path in OPTIONAL_HEAVY_PUBLIC_DENY_PATHS:\n        return False\n    return not any(path.startswith(prefix) for prefix in OPTIONAL_HEAVY_PUBLIC_DENY_PREFIXES)\n'''
new_boundary = '''SAFE_PUBLIC_METHODS = {"GET", "HEAD", "OPTIONS"}\nPUBLIC_AUTHORITY_SYMBOL = "BTCUSDT"\nPUBLIC_API_PATH_ALLOWLIST = frozenset({\n    "/",\n    "/openapi.json",\n    "/api/health",\n    "/healthz",\n    "/readyz",\n    "/api/oracle/score",\n    "/api/oracle/state",\n    "/api/oracle/predictions/db",\n    "/api/portfolio/live_gate",\n    "/api/authority/snapshot",\n    "/api/runtime/provenance",\n    "/api/market-context",\n})\nPUBLIC_STATIC_PREFIX = "/static/"\n\n\ndef _public_path_allowed(path: str) -> bool:\n    return path in PUBLIC_API_PATH_ALLOWLIST or path.startswith(PUBLIC_STATIC_PREFIX)\n'''
replace_once(main_path, old_boundary, new_boundary)
replace_once(
    main_path,
    '        await _snapshot("BTCUSDT", force=True)',
    '        await _refresh_snapshot(PUBLIC_AUTHORITY_SYMBOL)',
)
old_build = '''def _build_public_app() -> FastAPI:\n    public = FastAPI(\n        title="SENEX PUBLIC READ-ONLY",\n        version="ORDER-070-R1",\n        lifespan=real_lifespan,\n    )\n    # Copy only observational HTTP routes plus static/websocket transports.\n    # R6 closes the legacy GET inheritance hole: optional heavy endpoints are\n    # explicitly absent from production even though their HTTP method is safe.\n    for route in legacy.app.router.routes:\n        if isinstance(route, APIRoute):\n            methods = set(route.methods or set())\n            if (\n                route.path not in OVERRIDDEN_GET_PATHS\n                and methods <= SAFE_PUBLIC_METHODS\n                and _legacy_public_route_allowed(route.path)\n            ):\n                public.router.routes.append(route)\n        elif isinstance(route, (Mount, WebSocketRoute)):\n            public.router.routes.append(route)\n    return public\n\n\napp = _build_public_app()\n'''
new_build = '''def _build_public_app() -> FastAPI:\n    # Positive construction only: no legacy APIRoute/Mount/WebSocket inheritance.\n    return FastAPI(\n        title="SENEX PUBLIC READ-ONLY",\n        version="ORDER-070-R6",\n        lifespan=real_lifespan,\n        docs_url=None,\n        redoc_url=None,\n        openapi_url="/openapi.json",\n    )\n\n\napp = _build_public_app()\nFRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"\n\n\n@app.get("/")\nasync def public_dashboard():\n    index = FRONTEND_DIR / "index.html"\n    if index.exists():\n        return FileResponse(index)\n    return JSONResponse({"error": "frontend not built"}, status_code=404)\n\n\nif FRONTEND_DIR.exists():\n    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")\n'''
replace_once(main_path, old_build, new_build)
replace_once(
    main_path,
    '''    if request.method.upper() not in SAFE_PUBLIC_METHODS:\n        return JSONResponse(\n            {"detail": "PUBLIC_READ_ONLY_METHOD_DENIED"},\n            status_code=405,\n            headers={"X-Senex-Public-Decision": "DENY_UNSAFE_METHOD"},\n        )\n    response = await call_next(request)''',
    '''    if request.method.upper() not in SAFE_PUBLIC_METHODS:\n        return JSONResponse(\n            {"detail": "PUBLIC_READ_ONLY_METHOD_DENIED"},\n            status_code=405,\n            headers={"X-Senex-Public-Decision": "DENY_UNSAFE_METHOD"},\n        )\n    if not _public_path_allowed(request.url.path):\n        return JSONResponse(\n            {"detail": "PUBLIC_READ_ONLY_PATH_DENIED"},\n            status_code=404,\n            headers={"X-Senex-Public-Decision": "DENY_UNKNOWN_PATH"},\n        )\n    response = await call_next(request)''',
)
old_loop = '''def _authority_refresh_delay(interval_s: float, elapsed_s: float) -> float:\n    """Keep refresh attempt cadence start-to-start so capture latency cannot consume TTL headroom."""\n    return max(0.1, float(interval_s) - max(0.0, float(elapsed_s)))\n\n\nasync def _authority_refresh_loop(symbol: str = "BTCUSDT") -> None:\n    interval = authority_store.refresh_interval_s()\n    loop = asyncio.get_running_loop()\n    # Startup already performs one forced capture; preserve the initial spacing.\n    await asyncio.sleep(interval)\n    while True:\n        started = loop.time()\n        try:\n            await _snapshot(symbol, force=True)\n        except asyncio.CancelledError:\n            raise\n        except Exception as exc:\n            # The store records failure separately and retains last-known-good.\n            log.warning("authority snapshot refresh failed for %s: %s", symbol, exc)\n        elapsed = max(0.0, loop.time() - started)\n        await asyncio.sleep(_authority_refresh_delay(interval, elapsed))\n\n\nasync def _snapshot(symbol: str = "BTCUSDT", *, force: bool = False):\n    return await authority_store.get(\n        normalize_symbol(symbol),\n        live_gate_builder=_live_gate_from_score,\n        force=force,\n    )\n'''
new_loop = '''def _authority_refresh_delay(interval_s: float, elapsed_s: float) -> float:\n    """Keep scheduled refresh cadence start-to-start without increasing frequency."""\n    return max(1.0, float(interval_s) - max(0.0, float(elapsed_s)))\n\n\ndef _public_authority_symbol(symbol: str | None) -> str:\n    normalized = normalize_symbol(symbol or PUBLIC_AUTHORITY_SYMBOL)\n    if normalized != PUBLIC_AUTHORITY_SYMBOL:\n        raise HTTPException(status_code=404, detail="PUBLIC_AUTHORITY_SYMBOL_NOT_ALLOWED")\n    return normalized\n\n\nasync def _refresh_snapshot(symbol: str = PUBLIC_AUTHORITY_SYMBOL):\n    """The sole network/cache writer; called only by controlled lifecycle code."""\n    return await authority_store.get(\n        _public_authority_symbol(symbol),\n        live_gate_builder=_live_gate_from_score,\n        force=True,\n    )\n\n\nasync def _snapshot(symbol: str = PUBLIC_AUTHORITY_SYMBOL, *, force: bool = False):\n    """Compatibility writer alias retained for deterministic internal tests only."""\n    if not force:\n        snap, _ = _observed_snapshot(symbol)\n        return snap\n    return await _refresh_snapshot(symbol)\n\n\ndef _observed_snapshot(symbol: str | None = PUBLIC_AUTHORITY_SYMBOL):\n    """Pure public observation: zero DB I/O and zero snapshot/control mutation."""\n    normalized = _public_authority_symbol(symbol)\n    snap, refresh = authority_store.observe(normalized)\n    if snap is None:\n        raise HTTPException(status_code=503, detail="NO_VALID_AUTHORITY_GENERATION")\n    if refresh.get("snapshot_stale") is not False:\n        raise HTTPException(status_code=503, detail="AUTHORITY_SNAPSHOT_STALE")\n    if refresh.get("last_refresh_error") is not None:\n        raise HTTPException(status_code=503, detail="AUTHORITY_REFRESH_ERROR")\n    return snap, refresh\n\n\nasync def _authority_refresh_loop(symbol: str = PUBLIC_AUTHORITY_SYMBOL) -> None:\n    interval = authority_store.refresh_interval_s()\n    loop = asyncio.get_running_loop()\n    await asyncio.sleep(interval)\n    while True:\n        started = loop.time()\n        try:\n            await _refresh_snapshot(symbol)\n        except asyncio.CancelledError:\n            raise\n        except Exception as exc:\n            log.warning("authority snapshot refresh failed for %s: %s", symbol, exc)\n        elapsed = max(0.0, loop.time() - started)\n        await asyncio.sleep(_authority_refresh_delay(interval, elapsed))\n'''
replace_once(main_path, old_loop, new_loop)
# Public authority handlers become pure cache observers.
replace_once(
    main_path,
    '''@app.get("/api/oracle/score")\nasync def public_authoritative_oracle_score(symbol: str = Query(default="BTCUSDT")):\n    snap = await _snapshot(symbol)\n    return dict(snap.score)''',
    '''@app.get("/api/oracle/score")\nasync def public_authoritative_oracle_score(symbol: str = Query(default=PUBLIC_AUTHORITY_SYMBOL)):\n    snap, _ = _observed_snapshot(symbol)\n    return dict(snap.score)''',
)
replace_once(
    main_path,
    '''@app.get("/api/portfolio/live_gate")\nasync def public_live_gate(symbol: str = Query(default="BTCUSDT")):\n    snap = await _snapshot(symbol)\n    return dict(snap.live_gate)''',
    '''@app.get("/api/portfolio/live_gate")\nasync def public_live_gate(symbol: str = Query(default=PUBLIC_AUTHORITY_SYMBOL)):\n    snap, _ = _observed_snapshot(symbol)\n    return dict(snap.live_gate)''',
)
replace_once(
    main_path,
    '''@app.get("/api/oracle/state")\nasync def public_oracle_state(symbol: str = Query(default="BTCUSDT")):\n    snap = await _snapshot(symbol)''',
    '''@app.get("/api/oracle/state")\nasync def public_oracle_state(symbol: str = Query(default=PUBLIC_AUTHORITY_SYMBOL)):\n    snap, _ = _observed_snapshot(symbol)''',
)
replace_once(
    main_path,
    '''@app.get("/api/authority/snapshot")\nasync def public_authority_snapshot(symbol: str = Query(default="BTCUSDT")):\n    snap = await _snapshot(symbol)\n    return snap.to_dict(authority_store.refresh_status(snap.symbol))''',
    '''@app.get("/api/authority/snapshot")\nasync def public_authority_snapshot(symbol: str = Query(default=PUBLIC_AUTHORITY_SYMBOL)):\n    snap, refresh = _observed_snapshot(symbol)\n    return snap.to_dict(refresh)''',
)
replace_once(
    main_path,
    '''@app.get("/readyz")\nasync def readyz(symbol: str = Query(default="BTCUSDT")):\n    """Observational fail-closed readiness over the current shared generation."""\n    normalized = normalize_symbol(symbol)''',
    '''@app.get("/readyz")\nasync def readyz(symbol: str = Query(default=PUBLIC_AUTHORITY_SYMBOL)):\n    """Observational fail-closed readiness over the current shared generation."""\n    try:\n        normalized = _public_authority_symbol(symbol)\n    except HTTPException as exc:\n        return JSONResponse({"status": "not_ready", "probe": "readiness", "reason": exc.detail}, status_code=503)''',
)
replace_once(
    main_path,
    '''@app.get("/api/market-context")\nasync def market_context(symbol: str = Query(default="BTCUSDT")):\n    snap = await _snapshot(symbol)''',
    '''@app.get("/api/market-context")\nasync def market_context(symbol: str = Query(default=PUBLIC_AUTHORITY_SYMBOL)):\n    snap, _ = _observed_snapshot(symbol)''',
)
# F4: explicit bounded BTC-only DB evidence route. Exact total comes from the current complete snapshot;
# the feed itself is one bounded read and never performs a second count query.
marker = '''@app.get("/api/oracle/score")\nasync def public_authoritative_oracle_score'''
insert = '''@app.get("/api/oracle/predictions/db")\nasync def public_oracle_predictions_db(\n    limit: int = Query(default=50, ge=1, le=50),\n    symbol: str = Query(default=PUBLIC_AUTHORITY_SYMBOL),\n):\n    normalized = _public_authority_symbol(symbol)\n    snap, _ = _observed_snapshot(normalized)\n    from . import supabase_client\n    try:\n        rows = await supabase_client.fetch_predictions(limit=limit, symbol=normalized)\n    except Exception as exc:\n        log.warning("bounded public predictions read failed: %s", type(exc).__name__)\n        return JSONResponse(\n            {\n                "source": "supabase", "status": "unavailable", "symbol": normalized,\n                "limit": limit, "bounded": True, "count": 0, "total_in_db": None,\n                "exact_total_complete": False, "predictions": [],\n            },\n            status_code=503,\n        )\n    return {\n        "source": "supabase", "status": "ok", "symbol": normalized,\n        "limit": limit, "bounded": True, "count": len(rows),\n        "total_in_db": snap.exact_total_predictions,\n        "exact_total_complete": snap.exact_count_complete,\n        "predictions": rows,\n    }\n\n\n@app.get("/api/oracle/score")\nasync def public_authoritative_oracle_score'''
src = read(main_path)
if src.count(marker) != 1:
    raise RuntimeError("SCORE_INSERT_ANCHOR_DRIFT")
write(main_path, src.replace(marker, insert, 1))

# F2: background full-history refresh is bounded to the real 15m oracle cadence by default.
auth_path = "senecio_polymarket/backend/authority_snapshot.py"
old_init = '''    def __init__(self, ttl_s: float | None = None) -> None:\n        self.ttl_s = float(\n            ttl_s\n            if ttl_s is not None\n            else os.environ.get("SENEX_AUTHORITY_SNAPSHOT_TTL_SEC", "10")\n        )\n        self._cache: dict[str, AuthoritySnapshot] = {}'''
new_init = '''    def __init__(\n        self,\n        ttl_s: float | None = None,\n        refresh_interval_s: float | None = None,\n        capture_timeout_s: float | None = None,\n    ) -> None:\n        explicit_ttl = ttl_s is not None\n        configured_ttl = float(\n            ttl_s if explicit_ttl else os.environ.get("SENEX_AUTHORITY_SNAPSHOT_TTL_SEC", "1020")\n        )\n        self.capture_timeout_s = float(\n            capture_timeout_s\n            if capture_timeout_s is not None\n            else os.environ.get("SENEX_AUTHORITY_CAPTURE_TIMEOUT_SEC", "60")\n        )\n        if refresh_interval_s is not None:\n            interval = float(refresh_interval_s)\n        elif explicit_ttl:\n            interval = max(1.0, min(5.0, configured_ttl / 2.0))\n        else:\n            interval = float(os.environ.get("SENEX_AUTHORITY_REFRESH_INTERVAL_SEC", "900"))\n        if interval < 300.0 and not explicit_ttl:\n            interval = 300.0\n        # Production/global defaults always retain bounded capture-time headroom.\n        self.ttl_s = configured_ttl if explicit_ttl else max(\n            configured_ttl, interval + self.capture_timeout_s + 60.0\n        )\n        self._refresh_interval_s = interval\n        self._cache: dict[str, AuthoritySnapshot] = {}'''
replace_once(auth_path, old_init, new_init)
replace_once(
    auth_path,
    '''    def refresh_interval_s(self) -> float:\n        ttl = max(0.0, self.ttl_s)\n        if ttl <= 0.0:\n            return 1.0\n        return max(1.0, min(5.0, ttl / 2.0))''',
    '''    def refresh_interval_s(self) -> float:\n        return max(1.0, float(self._refresh_interval_s))''',
)
replace_once(
    auth_path,
    '                captured = await self._capture_complete(normalized, live_gate_builder)',
    '                captured = await asyncio.wait_for(\n                    self._capture_complete(normalized, live_gate_builder),\n                    timeout=max(1.0, self.capture_timeout_s),\n                )',
)

# F3: explicit canonical runtime import; no build-time overlay.
runner_path = "senecio_polymarket/backend/oracle_runner.py"
replace_once(
    runner_path,
    '        from predict_only import fetch_market_snapshot, run_prediction, log_prediction, check_candle_duplicate',
    '        from oracle_runtime.predict_only import (\n            fetch_market_snapshot, run_prediction, log_prediction, check_candle_duplicate,\n        )',
)

# F3: one canonical root Dockerfile, non-root, minimum writable ownership, exact provenance args.
root_docker = '''# SENEX ORDER-070 — single canonical production image definition\nFROM python:3.11-slim\n\nRUN apt-get update && apt-get install -y --no-install-recommends curl \\\n    && rm -rf /var/lib/apt/lists/* \\\n    && groupadd --system senex \\\n    && useradd --system --gid senex --home-dir /app --shell /usr/sbin/nologin senex\n\nWORKDIR /app\nCOPY senecio_polymarket/requirements.lock ./requirements.lock\nRUN pip install --no-cache-dir --require-hashes -r requirements.lock\n\nARG SENEX_SOURCE_COMMIT=unknown\nARG SENEX_SOURCE_TREE=unknown\nARG SENEX_IMAGE_DIGEST=unknown\nARG SENEX_BUILD_DIGEST=unknown\nENV SENEX_SOURCE_COMMIT=${SENEX_SOURCE_COMMIT}\nENV SENEX_SOURCE_TREE=${SENEX_SOURCE_TREE}\nENV SENEX_IMAGE_DIGEST=${SENEX_IMAGE_DIGEST}\nENV SENEX_BUILD_DIGEST=${SENEX_BUILD_DIGEST}\nLABEL org.opencontainers.image.revision=${SENEX_SOURCE_COMMIT} \\\n      org.senex.source-tree=${SENEX_SOURCE_TREE} \\\n      org.senex.build-digest=${SENEX_BUILD_DIGEST}\n\nCOPY senecio_polymarket/backend ./backend\nCOPY senecio_polymarket/frontend ./frontend\nCOPY senecio_polymarket/oracle ./oracle\nCOPY senecio_polymarket/oracle_runtime ./oracle_runtime\nCOPY senecio_polymarket/start_single_authority.sh /start.sh\nCOPY senecio_polymarket/start_single_authority.sh /app/start.sh\n\nRUN chmod 0555 /start.sh /app/start.sh \\\n    && mkdir -p /app/data/audit /app/oracle/senecio_output \\\n    && chown -R senex:senex /app/data /app/oracle/senecio_output \\\n    && chmod -R u=rwX,g=rX,o=rX /app/data /app/oracle/senecio_output\n\nENV PYTHONDONTWRITEBYTECODE=1\nENV PYTHONUNBUFFERED=1\nENV PYTHONPATH=/app\n\nEXPOSE 8080\nHEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \\\n  CMD curl -fsS http://localhost:8080/api/health || exit 1\n\nUSER senex\nCMD ["./start.sh"]\n'''
write("Dockerfile", root_docker)
nested = ROOT / "senecio_polymarket/Dockerfile"
if not nested.exists():
    raise RuntimeError("EXPECTED_NESTED_DOCKERFILE_MISSING_BEFORE_REMOVAL")
nested.unlink()

# Build digest must bind the actual root image definition and exact runtime inputs.
prov_path = "senecio_polymarket/backend/runtime_provenance.py"
old_files = '''    files = [\n        root / "senecio_polymarket" / "Dockerfile",\n        root / "senecio_polymarket" / "requirements.txt",\n        root / "senecio_polymarket" / "requirements.lock",\n        root / "senecio_polymarket" / "start_single_authority.sh",\n    ]'''
new_files = '''    files = [\n        root / "Dockerfile",\n        root / "senecio_polymarket" / "requirements.lock",\n        root / "senecio_polymarket" / "start_single_authority.sh",\n    ]'''
replace_once(prov_path, old_files, new_files)

# Docs follow the actual build truth.
docs_path = "senecio_polymarket/docs/ORDER_070_RUNTIME_TRUTH.md"
replace_once(
    docs_path,
    "Build provenance requires exact source commit, source tree, image digest, and canonical build digest. The canonical production image definition is `senecio_polymarket/Dockerfile`; dependency installation uses `requirements.lock` with hashes. The root Dockerfile remains a compatibility build entrypoint and uses the same locked dependency/runtime bridge contract.",
    "Build provenance requires exact source commit, source tree, image digest, and canonical build digest. The repository-root `Dockerfile` is the single canonical production image definition; `senecio_polymarket/Dockerfile` is removed. Dependency installation uses `requirements.lock` with hashes. The runtime imports `oracle_runtime.predict_only` explicitly; no build-time predictor mv/cp overlay exists. The container runs as non-root `senex`, with write ownership limited to `/app/data` and `/app/oracle/senecio_output`.",
)

# F4: edge and dashboard use the same explicit bounded BTC evidence route.
worker_path = "edge/order070/worker.js"
replace_once(
    worker_path,
    '  "/api/oracle/score", "/api/oracle/state", "/api/portfolio/live_gate",',
    '  "/api/oracle/score", "/api/oracle/state", "/api/oracle/predictions/db", "/api/portfolio/live_gate",',
)
appjs_path = "senecio_polymarket/frontend/app.js"
replace_once(
    appjs_path,
    "      const payload = await getJSON('/api/oracle/predictions/db?limit=50');",
    "      const payload = await getJSON('/api/oracle/predictions/db?limit=50&symbol=BTCUSDT');",
)
replace_once(
    appjs_path,
    "    $('#oracle-pred-meta').textContent = `[API_DERIVED] ${payload.total_in_db ?? 'UNKNOWN'} total_in_db · CROSS-SYMBOL · showing ${rows.length}`;",
    "    $('#oracle-pred-meta').textContent = `[API_DERIVED] ${payload.total_in_db ?? 'UNKNOWN'} total_in_db · BTCUSDT · bounded ${payload.limit ?? 50} · showing ${rows.length}`;",
)
replace_once(
    appjs_path,
    "    else clearDecisionContext('No BTC decision in the current cross-symbol predictions window');",
    "    else clearDecisionContext('No BTC decision in the current bounded BTCUSDT predictions window');",
)
old_intervals = '''  renderDomainHealth();\n  refreshContext();\n  refreshOracle();\n  setInterval(refreshContext, 2000);\n  setInterval(refreshOracle, 10000);\n  setInterval(renderDomainHealth, 1000);'''
new_intervals = '''  renderDomainHealth();\n  refreshContext();\n  refreshScore();\n  refreshPredictions();\n  setInterval(refreshContext, 2000);\n  setInterval(refreshScore, 10000);\n  setInterval(refreshPredictions, 60000);\n  setInterval(renderDomainHealth, 1000);'''
replace_once(appjs_path, old_intervals, new_intervals)

# F1-F4 independent contract tests. Expected public paths are not imported from implementation constants.
test_path = "senecio_polymarket/tests/test_order_070_r6_public_boundary.py"
write(test_path, '''from __future__ import annotations\n\nimport asyncio\nimport json\nimport os\nimport unittest\nfrom pathlib import Path\nfrom unittest import mock\n\nfrom fastapi.routing import APIRoute\nfrom fastapi.testclient import TestClient\nfrom starlette.routing import Mount, WebSocketRoute\n\nfrom backend import main_real, supabase_client\nfrom backend.authority_snapshot import AuthoritySnapshotStore\n\nROOT = Path(__file__).resolve().parents[2]\nEXPECTED_PUBLIC_APIRoutes = {\n    "/", "/healthz", "/readyz", "/api/health",\n    "/api/oracle/state", "/api/oracle/score", "/api/oracle/predictions/db",\n    "/api/portfolio/live_gate", "/api/authority/snapshot",\n    "/api/runtime/provenance", "/api/market-context",\n}\nPROVENANCE = {\n    "contract": "senex-runtime-provenance-v1",\n    "source_commit": "a" * 40, "source_tree": "b" * 40,\n    "image_digest": "sha256:" + "c" * 64, "build_digest": "sha256:" + "d" * 64,\n    "computed_build_digest": "sha256:" + "d" * 64,\n    "checks": {\n        "commit_exact": True, "tree_exact": True, "image_digest_exact": True,\n        "build_digest_exact": True, "build_digest_matches_runtime_files": True,\n    },\n    "exact": True,\n}\nROW = {"id": 1, "symbol": "BTCUSDT", "horizon": "1h", "outcome": "WIN", "ts": "2026-01-01T00:00:00Z", "audit": {}}\n\n\ndef gate(score):\n    return {\n        "trade_mode": "PAPER", "live_capital_locked": True, "orders_enabled": False,\n        "unlocked": False, "effective_gate": "LOCKED_BY_PAPER_POLICY",\n        "verified": int(score.get("independent_1h_rows") or 0),\n    }\n\n\nclass Order070R6PublicBoundaryTests(unittest.TestCase):\n    @staticmethod\n    def api_paths():\n        return {r.path for r in main_real.app.router.routes if isinstance(r, APIRoute)}\n\n    def test_public_api_route_set_equals_independent_positive_allowlist(self):\n        self.assertEqual(self.api_paths(), EXPECTED_PUBLIC_APIRoutes)\n        mounts = {r.path for r in main_real.app.router.routes if isinstance(r, Mount)}\n        self.assertEqual(mounts, {"/static"})\n        self.assertFalse([r for r in main_real.app.router.routes if isinstance(r, WebSocketRoute)])\n\n    def test_public_openapi_equals_independent_allowlist(self):\n        self.assertEqual(set(main_real.app.openapi().get("paths", {})), EXPECTED_PUBLIC_APIRoutes)\n\n    def test_unknown_and_heavy_legacy_paths_fail_closed(self):\n        client = TestClient(main_real.app)\n        for path in ("/__unknown__", "/api/research", "/api/research/report", "/api/antifragility/status", "/api/observability", "/metrics", "/ws", "/sse"):\n            response = client.get(path)\n            self.assertEqual(response.status_code, 404, path)\n            self.assertEqual(response.headers.get("x-senex-public-decision"), "DENY_UNKNOWN_PATH", path)\n\n    def test_public_authority_get_storm_has_zero_refresh_io_or_generation_mutation(self):\n        store = AuthoritySnapshotStore(ttl_s=60)\n        async def seed():\n            return await store.get("BTCUSDT", live_gate_builder=gate, force=True)\n        with mock.patch.object(supabase_client, "fetch_authority_history", return_value=[dict(ROW)]), \\\n             mock.patch.object(supabase_client, "count_predictions_exact", return_value=1414), \\\n             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(PROVENANCE)):\n            original = asyncio.run(seed())\n        original_generation = original.generation\n        original_refresh = json.dumps(store._refresh, sort_keys=True, default=str)\n        with mock.patch.object(main_real, "authority_store", store), \\\n             mock.patch.object(main_real.oracle_runner, "get_state", return_value={"started_at": "2026-08-22T00:00:00Z"}), \\\n             mock.patch.object(supabase_client, "fetch_authority_history", side_effect=AssertionError("public GET performed history refresh")) as history, \\\n             mock.patch.object(supabase_client, "count_predictions_exact", side_effect=AssertionError("public GET performed exact count")) as count:\n            for _ in range(20):\n                score = asyncio.run(main_real.public_authoritative_oracle_score("BTCUSDT"))\n                state = asyncio.run(main_real.public_oracle_state("BTCUSDT"))\n                live_gate = asyncio.run(main_real.public_live_gate("BTCUSDT"))\n                snapshot = asyncio.run(main_real.public_authority_snapshot("BTCUSDT"))\n                context = asyncio.run(main_real.market_context("BTCUSDT"))\n                self.assertEqual(score["authority_generation"], original_generation)\n                self.assertEqual(state["authority_generation"], original_generation)\n                self.assertEqual(live_gate["authority_generation"], original_generation)\n                self.assertEqual(snapshot["generation"], original_generation)\n                self.assertEqual(context["authority_generation"], original_generation)\n            self.assertEqual(history.call_count, 0)\n            self.assertEqual(count.call_count, 0)\n        self.assertEqual(store._generation["BTCUSDT"], original_generation)\n        self.assertEqual(json.dumps(store._refresh, sort_keys=True, default=str), original_refresh)\n\n    def test_stale_public_authority_fails_closed_without_refresh(self):\n        store = AuthoritySnapshotStore(ttl_s=1)\n        with mock.patch.object(supabase_client, "fetch_authority_history", return_value=[dict(ROW)]), \\\n             mock.patch.object(supabase_client, "count_predictions_exact", return_value=1414), \\\n             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(PROVENANCE)):\n            snap = asyncio.run(store.get("BTCUSDT", live_gate_builder=gate, force=True))\n        store._refresh["BTCUSDT"]["last_refresh_success_monotonic"] -= 10\n        generation = snap.generation\n        with mock.patch.object(main_real, "authority_store", store), \\\n             mock.patch.object(supabase_client, "fetch_authority_history", side_effect=AssertionError("stale GET refreshed")) as history, \\\n             mock.patch.object(supabase_client, "count_predictions_exact", side_effect=AssertionError("stale GET counted")) as count:\n            with self.assertRaises(Exception) as cm:\n                asyncio.run(main_real.public_authoritative_oracle_score("BTCUSDT"))\n            self.assertEqual(getattr(cm.exception, "status_code", None), 503)\n            self.assertEqual(history.call_count, 0)\n            self.assertEqual(count.call_count, 0)\n        self.assertEqual(store._generation["BTCUSDT"], generation)\n\n    def test_default_refresh_policy_is_bounded_and_has_capture_headroom(self):\n        with mock.patch.dict(os.environ, {}, clear=True):\n            store = AuthoritySnapshotStore()\n        self.assertGreaterEqual(store.refresh_interval_s(), 300.0)\n        self.assertGreater(store.ttl_s, store.refresh_interval_s() + store.capture_timeout_s)\n\n    def test_background_complete_refresh_is_the_only_writer(self):\n        store = AuthoritySnapshotStore(ttl_s=60)\n        with mock.patch.object(supabase_client, "fetch_authority_history", return_value=[dict(ROW)]) as history, \\\n             mock.patch.object(supabase_client, "count_predictions_exact", return_value=1414) as count, \\\n             mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(PROVENANCE)):\n            snap = asyncio.run(store.get("BTCUSDT", live_gate_builder=gate, force=True))\n        self.assertTrue(snap.authority_history_complete)\n        self.assertTrue(snap.exact_count_complete)\n        self.assertEqual(history.call_count, 1)\n        self.assertEqual(count.call_count, 1)\n\n    def test_build_contract_is_root_only_nonroot_and_overlay_free(self):\n        docker = (ROOT / "Dockerfile").read_text()\n        self.assertFalse((ROOT / "senecio_polymarket/Dockerfile").exists())\n        self.assertNotIn("mv /app/oracle/predict_only.py", docker)\n        self.assertNotIn("cp /app/oracle_runtime/predict_only.py", docker)\n        self.assertNotIn("chmod -R 777", docker)\n        self.assertIn("USER senex", docker)\n        self.assertIn("--require-hashes -r requirements.lock", docker)\n        runner = (ROOT / "senecio_polymarket/backend/oracle_runner.py").read_text()\n        self.assertIn("from oracle_runtime.predict_only import", runner)\n        provenance = (ROOT / "senecio_polymarket/backend/runtime_provenance.py").read_text()\n        self.assertIn('root / "Dockerfile"', provenance)\n        self.assertNotIn('root / "senecio_polymarket" / "Dockerfile"', provenance)\n\n    def test_edge_dashboard_route_parity_and_bounded_poll_cadence(self):\n        worker = (ROOT / "edge/order070/worker.js").read_text()\n        frontend = (ROOT / "senecio_polymarket/frontend/app.js").read_text()\n        self.assertIn('"/api/oracle/predictions/db"', worker)\n        self.assertIn("/api/oracle/predictions/db?limit=50&symbol=BTCUSDT", frontend)\n        self.assertIn("setInterval(refreshPredictions, 60000)", frontend)\n        self.assertIn("setInterval(refreshContext, 2000)", frontend)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''')

# Existing ORDER-070 dependency-lock test must bind the root canonical Dockerfile.
order_test = "senecio_polymarket/tests/test_order_070.py"
replace_once(
    order_test,
    '        docker=(ROOT/"senecio_polymarket/Dockerfile").read_text()\n        self.assertIn("--require-hashes -r requirements.lock",docker)',
    '        docker=(ROOT/"Dockerfile").read_text()\n        self.assertFalse((ROOT/"senecio_polymarket/Dockerfile").exists())\n        self.assertIn("--require-hashes -r requirements.lock",docker)',
)

# F5: artifact identity comes from the checked-out exact HEAD; R6 hard gates run explicitly.
workflow_path = ".github/workflows/senex-order-070.yml"
workflow = '''name: SENEX ORDER-070 Runtime Truth\non:\n  pull_request:\n    branches: [main]\n  workflow_dispatch:\npermissions:\n  contents: read\njobs:\n  order070:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          ref: ${{ github.event.pull_request.head.sha || github.sha }}\n          fetch-depth: 0\n      - name: Resolve exact checked-out identity\n        shell: bash\n        run: |\n          echo "ORDER070_EXACT_HEAD=$(git rev-parse HEAD)" >> "$GITHUB_ENV"\n          echo "ORDER070_EXACT_TREE=$(git rev-parse HEAD^{tree})" >> "$GITHUB_ENV"\n      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.11'\n          cache: pip\n          cache-dependency-path: senecio_polymarket/requirements.lock\n      - name: Install exact locked runtime\n        run: python -m pip install --require-hashes -r senecio_polymarket/requirements.lock\n      - name: ORDER-070 R6 consolidated hard gates\n        env:\n          PYTHONPATH: senecio_polymarket\n        run: python -m unittest -v senecio_polymarket.tests.test_order_070_r6_public_boundary\n      - name: ORDER-070 regression\n        env:\n          PYTHONPATH: senecio_polymarket\n        run: python -m unittest -v senecio_polymarket.tests.test_order_070\n      - name: Full regression suite\n        env:\n          PYTHONPATH: senecio_polymarket\n        run: python -m unittest discover -s senecio_polymarket/tests -p 'test_*.py'\n      - name: Secret scan including Markdown\n        run: python senecio_polymarket/scripts/order070_secret_scan.py\n      - name: Public OpenAPI zero mutation proof\n        env:\n          PYTHONPATH: senecio_polymarket\n        run: |\n          python - <<'PY'\n          from fastapi.routing import APIRoute\n          from backend.main_real import app\n          routes=[r for r in app.router.routes if isinstance(r,APIRoute)]\n          posts=sum('POST' in (r.methods or set()) for r in routes)\n          print(f'PUBLIC_FASTAPI_POST_COUNT={posts}')\n          assert posts == 0\n          PY\n      - name: Canonical root build contract proof\n        shell: bash\n        run: |\n          test -f Dockerfile\n          test ! -e senecio_polymarket/Dockerfile\n          ! grep -q 'mv /app/oracle/predict_only.py' Dockerfile\n          ! grep -q 'cp /app/oracle_runtime/predict_only.py' Dockerfile\n          ! grep -q 'chmod -R 777' Dockerfile\n          grep -q '^USER senex$' Dockerfile\n      - name: Seal exact-head artifact\n        run: python senecio_polymarket/scripts/order070_seal.py | tee /tmp/order070-artifact-sha.txt\n      - uses: actions/upload-artifact@v4\n        with:\n          name: order070-sealed-${{ env.ORDER070_EXACT_HEAD }}\n          path: senecio_polymarket/artifacts/order070-sealed.json\n          if-no-files-found: error\n'''
write(workflow_path, workflow)

print("ORDER070_R6_CONSOLIDATED_MATERIALIZATION=APPLIED_LOCAL_ONLY")
