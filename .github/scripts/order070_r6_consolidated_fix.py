from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path.cwd()
EXPECTED_HEAD = "b438c0d6dc156d4183929366963df988d97a5283"
EXPECTED_TREE = "e1e4b002aa90402517edb90412b755bc8529327e"


def read(path: str) -> str:
    return (ROOT / path).read_text()


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected one occurrence, got {text.count(old)}")
    return text.replace(old, new, 1)


def regex_once(text: str, pattern: str, replacement: str, label: str) -> str:
    out, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected one regex replacement, got {count}")
    return out


# F1/F2/F4 — explicit positive public surface, observational authority reads,
# bounded lifecycle-only complete refresh, and bounded cached DB evidence.
p = "senecio_polymarket/backend/main_real.py"
s = read(p)
s = replace_once(s, "import os\nfrom contextlib", "import os\nfrom pathlib import Path\nfrom contextlib", "main_real pathlib")
s = replace_once(s, "from fastapi import FastAPI, Query, Request", "from fastapi import FastAPI, HTTPException, Query, Request", "main_real HTTPException")
s = replace_once(s, "from fastapi.responses import JSONResponse", "from fastapi.responses import FileResponse, JSONResponse\nfrom fastapi.staticfiles import StaticFiles", "main_real static imports")
s = s.replace("from fastapi.routing import APIRoute\n", "").replace("from starlette.routing import Mount, WebSocketRoute\n", "")
s = regex_once(
    s,
    r'SAFE_PUBLIC_METHODS = \{"GET", "HEAD", "OPTIONS"\}.*?\n\ndef synthetic_demo_enabled',
    '''SAFE_PUBLIC_METHODS = {"GET", "HEAD", "OPTIONS"}\nPUBLIC_AUTHORITY_SYMBOL = "BTCUSDT"\nFRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"\n\n\ndef _validate_public_symbol(value: str | None) -> str:\n    normalized = normalize_symbol(value)\n    if normalized != PUBLIC_AUTHORITY_SYMBOL:\n        raise HTTPException(status_code=404, detail="PUBLIC_AUTHORITY_SYMBOL_NOT_ALLOWED")\n    return normalized\n\n\ndef synthetic_demo_enabled''',
    "main_real explicit boundary constants",
)
s = regex_once(
    s,
    r'def _build_public_app\(\) -> FastAPI:.*?\n\n\napp = _build_public_app\(\)',
    '''def _build_public_app() -> FastAPI:\n    public = FastAPI(\n        title="SENEX PUBLIC READ-ONLY",\n        version="ORDER-070-R6",\n        lifespan=real_lifespan,\n    )\n    if FRONTEND_DIR.exists():\n        public.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")\n    return public\n\n\napp = _build_public_app()\n\n\n@app.get("/", include_in_schema=False)\nasync def dashboard_root():\n    index = FRONTEND_DIR / "index.html"\n    if index.exists():\n        return FileResponse(index)\n    return JSONResponse({"error": "frontend not built"}, status_code=404)''',
    "main_real public app allowlist",
)
old_snapshot = '''async def _snapshot(symbol: str = "BTCUSDT", *, force: bool = False):\n    return await authority_store.get(\n        normalize_symbol(symbol),\n        live_gate_builder=_live_gate_from_score,\n        force=force,\n    )\n'''
new_snapshot = '''async def _snapshot(symbol: str = "BTCUSDT", *, force: bool = False):\n    normalized = _validate_public_symbol(symbol)\n    if force:\n        return await authority_store.get(\n            normalized,\n            live_gate_builder=_live_gate_from_score,\n            force=True,\n        )\n    snap, refresh = authority_store.observe(normalized)\n    if snap is None:\n        raise HTTPException(status_code=503, detail="NO_VALID_AUTHORITY_GENERATION")\n    if refresh.get("snapshot_stale") or refresh.get("last_refresh_error") is not None:\n        raise HTTPException(status_code=503, detail="AUTHORITY_GENERATION_STALE_OR_ERROR")\n    return snap\n'''
s = replace_once(s, old_snapshot, new_snapshot, "main_real observational snapshot")
s = replace_once(s, "    normalized = normalize_symbol(symbol)\n    try:\n        snap, refresh = authority_store.observe(normalized)", "    normalized = _validate_public_symbol(symbol)\n    try:\n        snap, refresh = authority_store.observe(normalized)", "ready symbol allowlist")
anchor = '''@app.get("/api/authority/snapshot")\nasync def public_authority_snapshot(symbol: str = Query(default="BTCUSDT")):\n    snap = await _snapshot(symbol)\n    return snap.to_dict(authority_store.refresh_status(snap.symbol))\n'''
addition = anchor + '''\n\n@app.get("/api/oracle/predictions/db")\nasync def public_predictions_db(\n    limit: int = Query(default=50, ge=1, le=50),\n    symbol: str = Query(default="BTCUSDT"),\n):\n    snap = await _snapshot(symbol)\n    rows = authority_store.recent_predictions(snap.symbol, limit=limit)\n    return {\n        "source": "authority_snapshot_cache",\n        "symbol": snap.symbol,\n        "bounded": True,\n        "limit": int(limit),\n        "count": len(rows),\n        "total_in_db": snap.exact_total_predictions,\n        "exact_count_complete": snap.exact_count_complete,\n        "authority_snapshot_id": snap.snapshot_id,\n        "authority_generation": snap.generation,\n        "authority_canonical_sha256": snap.canonical_sha256,\n        "predictions": rows,\n    }\n'''
s = replace_once(s, anchor, addition, "main_real cached predictions route")
ast.parse(s)
write(p, s)

# F2 — runtime default: one complete authority capture no more frequently than
# every 300s, with >=120s freshness headroom. Public routes never call get().
p = "senecio_polymarket/backend/authority_snapshot.py"
s = read(p)
old_init = '''    def __init__(self, ttl_s: float | None = None) -> None:\n        self.ttl_s = float(\n            ttl_s\n            if ttl_s is not None\n            else os.environ.get("SENEX_AUTHORITY_SNAPSHOT_TTL_SEC", "10")\n        )\n        self._cache: dict[str, AuthoritySnapshot] = {}\n        self._locks: dict[str, asyncio.Lock] = {}\n        self._generation: dict[str, int] = {}\n        self._refresh: dict[str, dict[str, Any]] = {}\n'''
new_init = '''    def __init__(self, ttl_s: float | None = None) -> None:\n        self._runtime_policy = ttl_s is None\n        self._refresh_period_s = max(\n            300.0, float(os.environ.get("SENEX_AUTHORITY_REFRESH_INTERVAL_SEC", "300"))\n        )\n        if ttl_s is None:\n            configured_ttl = float(os.environ.get("SENEX_AUTHORITY_SNAPSHOT_TTL_SEC", "600"))\n            self.ttl_s = max(configured_ttl, self._refresh_period_s + 120.0)\n        else:\n            self.ttl_s = float(ttl_s)\n        self._cache: dict[str, AuthoritySnapshot] = {}\n        self._locks: dict[str, asyncio.Lock] = {}\n        self._generation: dict[str, int] = {}\n        self._refresh: dict[str, dict[str, Any]] = {}\n        self._recent: dict[str, tuple[dict[str, Any], ...]] = {}\n'''
s = replace_once(s, old_init, new_init, "authority runtime cadence init")
s = replace_once(s, "        self._refresh.clear()\n", "        self._refresh.clear()\n        self._recent.clear()\n", "authority clear recent")
old_interval = '''    def refresh_interval_s(self) -> float:\n        ttl = max(0.0, self.ttl_s)\n        if ttl <= 0.0:\n            return 1.0\n        return max(1.0, min(5.0, ttl / 2.0))\n'''
new_interval = '''    def refresh_interval_s(self) -> float:\n        if self._runtime_policy:\n            return self._refresh_period_s\n        ttl = max(0.0, self.ttl_s)\n        if ttl <= 0.0:\n            return 1.0\n        return max(1.0, min(5.0, ttl / 2.0))\n'''
s = replace_once(s, old_interval, new_interval, "authority bounded interval")
marker = '''    def _fresh(self, symbol: str) -> bool:\n'''
method = '''    def recent_predictions(self, symbol: str, *, limit: int = 50) -> list[dict[str, Any]]:\n        """Return a bounded copy of the current lifecycle-captured rows; no I/O or mutation."""\n        normalized = normalize_symbol(symbol)\n        bounded = max(1, min(int(limit), 50))\n        return copy.deepcopy(list(self._recent.get(normalized, ()))[:bounded])\n\n'''
s = replace_once(s, marker, method + marker, "authority recent observational method")
s = replace_once(
    s,
    '''            if cached is not None and cached.canonical_sha256 == captured.canonical_sha256:\n                # Byte-equivalent authority content revalidates freshness without rotating identity.\n                self._mark_success(normalized)\n                return cached\n''',
    '''            self._recent[normalized] = tuple(copy.deepcopy(captured.recent_predictions))\n            if cached is not None and cached.canonical_sha256 == captured.canonical_sha256:\n                # Byte-equivalent authority content revalidates freshness without rotating identity.\n                self._mark_success(normalized)\n                return cached\n''',
    "authority cache recent on capture",
)
s = replace_once(s, "    provenance: dict[str, Any]\n\n\nclass AuthoritySnapshotStore", "    provenance: dict[str, Any]\n    recent_predictions: tuple[dict[str, Any], ...]\n\n\nclass AuthoritySnapshotStore", "captured authority recent field")
s = replace_once(
    s,
    '''        rows = _canonical_rows(history_result)\n        exact_total = int(count_result)\n        score = build_authoritative_score(rows, symbol=symbol)\n''',
    '''        rows = _canonical_rows(history_result)\n        recent_predictions = tuple(copy.deepcopy(list(reversed(rows[-50:]))))\n        exact_total = int(count_result)\n        score = build_authoritative_score(rows, symbol=symbol)\n''',
    "authority derive bounded recent",
)
s = replace_once(s, "            provenance=provenance,\n        )\n\n\nSTORE", "            provenance=provenance,\n            recent_predictions=recent_predictions,\n        )\n\n\nSTORE", "authority return recent")
ast.parse(s)
write(p, s)

# F3 — single root Dockerfile, non-root runtime, minimum writable ownership,
# and no build-time predictor overlay.
root_docker = '''# SENEX ORDER-070 — canonical production Dockerfile\nFROM python:3.11-slim\n\nRUN apt-get update && apt-get install -y --no-install-recommends curl \\\n    && rm -rf /var/lib/apt/lists/*\n\nWORKDIR /app\nCOPY senecio_polymarket/requirements.lock ./requirements.lock\nRUN pip install --no-cache-dir --require-hashes -r requirements.lock\n\nARG SENEX_SOURCE_COMMIT=unknown\nARG SENEX_SOURCE_TREE=unknown\nARG SENEX_IMAGE_DIGEST=unknown\nARG SENEX_BUILD_DIGEST=unknown\nENV SENEX_SOURCE_COMMIT=${SENEX_SOURCE_COMMIT}\nENV SENEX_SOURCE_TREE=${SENEX_SOURCE_TREE}\nENV SENEX_IMAGE_DIGEST=${SENEX_IMAGE_DIGEST}\nENV SENEX_BUILD_DIGEST=${SENEX_BUILD_DIGEST}\nLABEL org.opencontainers.image.revision=${SENEX_SOURCE_COMMIT} \\\n      org.senex.source-tree=${SENEX_SOURCE_TREE} \\\n      org.senex.build-digest=${SENEX_BUILD_DIGEST}\n\nCOPY senecio_polymarket/backend ./backend\nCOPY senecio_polymarket/frontend ./frontend\nCOPY senecio_polymarket/oracle ./oracle\nCOPY senecio_polymarket/oracle_runtime ./oracle_runtime\nCOPY senecio_polymarket/start_single_authority.sh /app/start.sh\nCOPY senecio_polymarket/start_single_authority.sh /start.sh\n\nRUN addgroup --system --gid 10001 senex \\\n    && adduser --system --uid 10001 --ingroup senex --home /app --no-create-home senex \\\n    && mkdir -p /app/data/audit /app/oracle/senecio_output \\\n    && chown -R senex:senex /app/data /app/oracle/senecio_output \\\n    && chmod -R u=rwX,g=rX,o= /app/data /app/oracle/senecio_output \\\n    && chmod 0555 /app/start.sh /start.sh\n\nENV PYTHONDONTWRITEBYTECODE=1\nENV PYTHONUNBUFFERED=1\nENV PYTHONPATH=/app\nEXPOSE 8080\nHEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \\\n  CMD curl -fsS http://localhost:8080/healthz || exit 1\nUSER senex:senex\nCMD ["./start.sh"]\n'''
write("Dockerfile", root_docker)
nested = ROOT / "senecio_polymarket/Dockerfile"
if not nested.exists():
    raise RuntimeError("nested Dockerfile unexpectedly absent before F3")
nested.unlink()

p = "senecio_polymarket/backend/oracle_runner.py"
s = read(p)
s = replace_once(
    s,
    "        from predict_only import fetch_market_snapshot, run_prediction, log_prediction, check_candle_duplicate",
    "        from oracle_runtime.predict_only import fetch_market_snapshot, run_prediction, log_prediction, check_candle_duplicate",
    "oracle explicit runtime import",
)
ast.parse(s)
write(p, s)

p = "senecio_polymarket/oracle_runtime/predict_only.py"
s = read(p)
s = replace_once(
    s,
    '''The Docker image preserves the original predictor as ``predict_only_base.py``\nand installs this module at ``/app/oracle/predict_only.py``. Every original\nfunction is re-exported unchanged except ``run_prediction``.\n''',
    '''The canonical runtime imports this bridge explicitly as\n``oracle_runtime.predict_only``. The frozen original predictor remains at\n``/app/oracle/predict_only.py`` and is loaded read-only; no build-time overlay is used.\n''',
    "runtime bridge docs",
)
s = replace_once(
    s,
    '''_BASE_PATH = _ORACLE_DIR / "predict_only_base.py"\nif not _BASE_PATH.exists():\n    _BASE_PATH = _ORACLE_DIR / "predict_only.py"\n''',
    '''_BASE_PATH = _ORACLE_DIR / "predict_only.py"\nif not _BASE_PATH.exists():\n    raise ImportError(f"canonical predictor missing: {_BASE_PATH}")\n''',
    "runtime bridge canonical base",
)
ast.parse(s)
write(p, s)

p = "senecio_polymarket/backend/runtime_provenance.py"
s = read(p)
s = replace_once(s, '        root / "senecio_polymarket" / "Dockerfile",\n', '        root / "Dockerfile",\n', "provenance root Dockerfile")
s = s.replace('        root / "senecio_polymarket" / "requirements.txt",\n', '')
ast.parse(s)
write(p, s)

write(
    "senecio_polymarket/docs/ORDER_070_RUNTIME_TRUTH.md",
    '''# ORDER-070 runtime truth contract\n\nThe production public process is `backend.main_real:app`. The public surface is a positive allowlist: root/static plus the explicitly enumerated observational APIs only. Unknown legacy GETs, legacy WebSocket inheritance, research, antifragility, observability, metrics, admin and every unsafe HTTP method are not mounted.\n\nAuthority is single-writer. A background lifecycle task performs complete BTCUSDT authority-history + exact-count captures at a bounded cadence of at least 300 seconds. Public score/state/live-gate/snapshot/market-context/prediction-feed GETs only observe the immutable cached generation and fail closed when no valid fresh generation exists; public GETs cannot trigger Supabase authority reads or rotate generation state.\n\n`/api/oracle/predictions/db?limit=50&symbol=BTCUSDT` is a bounded read-only view of rows already captured by the authority lifecycle. It performs no request-time Supabase query. The dashboard polls this evidence no faster than once per 60 seconds.\n\nThe repository-root `Dockerfile` is the sole canonical production image definition. `senecio_polymarket/Dockerfile` is retired. The image imports the runtime bridge explicitly through `oracle_runtime.predict_only`; the frozen predictor remains at `oracle/predict_only.py` and is never moved/copied over at build time. The runtime is non-root and only `/app/data` plus `/app/oracle/senecio_output` receive narrow owner write permission; no app path is world-writable.\n\nBuild provenance binds the exact source commit, source tree, OCI digest and a canonical build digest computed from the root Dockerfile, locked dependencies and launcher. No prediction thresholds, model weights or signal-generation rules are changed by ORDER-070.\n\nThe temporary Cloudflare Worker under `edge/order070/` is proof infrastructure only. It strips authorization/cookie headers, proxies only its explicit GET/HEAD allowlist (including the bounded BTC prediction view required by the dashboard), and denies unknown paths and unsafe methods before origin.\n\nPermanent runtime locks remain `trade_mode=PAPER`, `orders_enabled=false`, `live_capital_locked=true`.\n''',
)

# F4 — edge/dashboard parity and bounded DB evidence polling.
p = "edge/order070/worker.js"
s = read(p)
s = replace_once(
    s,
    '  "/api/authority/snapshot", "/api/runtime/provenance", "/api/market-context"\n',
    '  "/api/authority/snapshot", "/api/runtime/provenance", "/api/market-context",\n  "/api/oracle/predictions/db"\n',
    "edge dashboard route parity",
)
write(p, s)

p = "senecio_polymarket/frontend/app.js"
s = read(p)
s = replace_once(s, "getJSON('/api/oracle/predictions/db?limit=50')", "getJSON('/api/oracle/predictions/db?limit=50&symbol=BTCUSDT')", "dashboard BTC bounded route")
s = replace_once(s, "`[API_DERIVED] ${payload.total_in_db ?? 'UNKNOWN'} total_in_db · CROSS-SYMBOL · showing ${rows.length}`", "`[API_DERIVED] ${payload.total_in_db ?? 'UNKNOWN'} total_in_db · BTCUSDT · bounded cache · showing ${rows.length}`", "dashboard scoped label")
s = replace_once(
    s,
    '''  refreshContext();\n  refreshOracle();\n  setInterval(refreshContext, 2000);\n  setInterval(refreshOracle, 10000);\n  setInterval(renderDomainHealth, 1000);\n''',
    '''  refreshContext();\n  refreshScore();\n  refreshPredictions();\n  setInterval(refreshContext, 2000);\n  setInterval(refreshScore, 10000);\n  setInterval(refreshPredictions, 60000);\n  setInterval(renderDomainHealth, 1000);\n''',
    "dashboard bounded evidence cadence",
)
write(p, s)

# F5 — checked-out exact HEAD identifies the artifact; explicit R6 consolidated gate.
p = ".github/workflows/senex-order-070.yml"
s = read(p)
s = replace_once(
    s,
    "      - uses: actions/setup-python@v5\n",
    '''      - name: Capture checked-out exact identity\n        id: exact_identity\n        shell: bash\n        run: |\n          echo "head=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"\n          echo "tree=$(git rev-parse HEAD^{tree})" >> "$GITHUB_OUTPUT"\n      - uses: actions/setup-python@v5\n''',
    "workflow exact identity",
)
s = replace_once(
    s,
    '''      - name: Full regression suite\n        env:\n          PYTHONPATH: senecio_polymarket\n        run: python -m unittest discover -s senecio_polymarket/tests -p 'test_*.py'\n''',
    '''      - name: R6 consolidated acceptance F1-F4\n        env:\n          PYTHONPATH: senecio_polymarket\n        run: python -m unittest -v senecio_polymarket.tests.test_order_070_r6_public_boundary\n      - name: Full regression suite\n        env:\n          PYTHONPATH: senecio_polymarket\n        run: python -m unittest discover -s senecio_polymarket/tests -p 'test_*.py'\n''',
    "workflow consolidated gate",
)
s = replace_once(s, "          name: order070-sealed-${{ github.sha }}", "          name: order070-sealed-${{ steps.exact_identity.outputs.head }}", "workflow artifact exact head")
write(p, s)

# Independent acceptance oracle: expected public paths are literal here, not
# derived from production deny/allow constants.
write(
    "senecio_polymarket/tests/test_order_070_r6_public_boundary.py",
    '''from __future__ import annotations\n\nimport asyncio\nimport os\nimport time\nimport unittest\nfrom pathlib import Path\nfrom unittest import mock\n\nfrom fastapi import HTTPException\nfrom fastapi.routing import APIRoute\nfrom starlette.routing import Mount, WebSocketRoute\n\nfrom backend import main_real, supabase_client\nfrom backend.authority_snapshot import AuthoritySnapshotStore\n\nROOT = Path(__file__).resolve().parents[2]\nEXPECTED_PUBLIC_API_PATHS = {\n    "/",\n    "/api/health",\n    "/healthz",\n    "/readyz",\n    "/api/oracle/score",\n    "/api/oracle/state",\n    "/api/oracle/predictions/db",\n    "/api/portfolio/live_gate",\n    "/api/authority/snapshot",\n    "/api/runtime/provenance",\n    "/api/market-context",\n}\nROW = {"id": 1, "symbol": "BTCUSDT", "horizon": "1h", "outcome": "WIN", "ts": "2026-01-01T00:00:00Z", "audit": {}}\nPROVENANCE = {\n    "contract": "senex-runtime-provenance-v1",\n    "source_commit": "a" * 40, "source_tree": "b" * 40,\n    "image_digest": "sha256:" + "c" * 64, "build_digest": "sha256:" + "d" * 64,\n    "computed_build_digest": "sha256:" + "d" * 64,\n    "checks": {"commit_exact": True, "tree_exact": True, "image_digest_exact": True, "build_digest_exact": True, "build_digest_matches_runtime_files": True},\n    "exact": True,\n}\n\n\ndef gate(score):\n    return {"trade_mode": "PAPER", "live_capital_locked": True, "orders_enabled": False, "effective_gate": "LOCKED_BY_PAPER_POLICY", "unlocked": False}\n\n\nclass ConsolidatedR6Tests(unittest.TestCase):\n    def test_f1_public_route_set_equals_independent_positive_allowlist(self):\n        actual = {r.path for r in main_real.app.router.routes if isinstance(r, APIRoute)}\n        self.assertEqual(actual, EXPECTED_PUBLIC_API_PATHS)\n        self.assertFalse(any(isinstance(r, WebSocketRoute) for r in main_real.app.router.routes))\n        mounts = {r.path for r in main_real.app.router.routes if isinstance(r, Mount)}\n        self.assertEqual(mounts, {"/static"})\n\n    def test_f1_unknown_and_optional_legacy_routes_default_absent(self):\n        actual = {r.path for r in main_real.app.router.routes if isinstance(r, APIRoute)}\n        forbidden = {"/api/research/status", "/api/antifragility/status", "/api/observability", "/metrics", "/api/stats", "/api/audit", "/api/catalog"}\n        self.assertTrue(actual.isdisjoint(forbidden))\n\n    def test_f2_public_get_storm_causes_zero_supabase_refresh_and_zero_generation_mutation(self):\n        store = AuthoritySnapshotStore(ttl_s=60)\n        calls = {"history": 0, "count": 0}\n        async def history(symbol=None): calls["history"] += 1; return [dict(ROW)]\n        async def count(): calls["count"] += 1; return 9\n        async def run():\n            snap = await store.get("BTCUSDT", live_gate_builder=gate, force=True)\n            before = (snap.snapshot_id, snap.generation, store.refresh_status("BTCUSDT"))\n            with mock.patch.object(main_real, "authority_store", store), mock.patch.object(main_real.oracle_runner, "get_state", return_value={"started_at": "x"}):\n                for _ in range(20):\n                    await main_real.public_authority_snapshot("BTCUSDT")\n                    await main_real.public_authoritative_oracle_score("BTCUSDT")\n                    await main_real.public_live_gate("BTCUSDT")\n                    await main_real.public_oracle_state("BTCUSDT")\n                    await main_real.public_predictions_db(50, "BTCUSDT")\n            after_snap, after_status = store.observe("BTCUSDT")\n            return before, after_snap, after_status\n        with mock.patch.object(supabase_client, "fetch_authority_history", side_effect=history), mock.patch.object(supabase_client, "count_predictions_exact", side_effect=count), mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(PROVENANCE)):\n            before, after_snap, after_status = asyncio.run(run())\n        self.assertEqual(calls, {"history": 1, "count": 1})\n        self.assertEqual((after_snap.snapshot_id, after_snap.generation), before[:2])\n        self.assertEqual(after_status["last_refresh_attempt_at"], before[2]["last_refresh_attempt_at"])\n\n    def test_f2_stale_public_read_fails_closed_without_supabase_io(self):\n        store = AuthoritySnapshotStore(ttl_s=0.001)\n        with mock.patch.object(supabase_client, "fetch_authority_history", return_value=[dict(ROW)]), mock.patch.object(supabase_client, "count_predictions_exact", return_value=9), mock.patch("backend.authority_snapshot.runtime_provenance", return_value=dict(PROVENANCE)):\n            asyncio.run(store.get("BTCUSDT", live_gate_builder=gate, force=True))\n        time.sleep(0.01)\n        with mock.patch.object(main_real, "authority_store", store), mock.patch.object(supabase_client, "fetch_authority_history", side_effect=AssertionError("public GET performed DB read")), mock.patch.object(supabase_client, "count_predictions_exact", side_effect=AssertionError("public GET performed count")):\n            with self.assertRaises(HTTPException) as cm:\n                asyncio.run(main_real.public_authority_snapshot("BTCUSDT"))\n        self.assertEqual(cm.exception.status_code, 503)\n\n    def test_f2_runtime_refresh_is_bounded_and_has_ttl_headroom(self):\n        with mock.patch.dict(os.environ, {}, clear=False):\n            store = AuthoritySnapshotStore()\n        self.assertGreaterEqual(store.refresh_interval_s(), 300.0)\n        self.assertGreater(store.ttl_s, store.refresh_interval_s() + 60.0)\n\n    def test_f2_arbitrary_symbol_is_rejected_before_store_cardinality(self):\n        with self.assertRaises(HTTPException) as cm:\n            asyncio.run(main_real.public_authority_snapshot("ETHUSDT"))\n        self.assertEqual(cm.exception.status_code, 404)\n\n    def test_f3_single_root_canonical_build_nonroot_and_no_overlay(self):\n        docker = (ROOT / "Dockerfile").read_text()\n        runner = (ROOT / "senecio_polymarket/backend/oracle_runner.py").read_text()\n        provenance = (ROOT / "senecio_polymarket/backend/runtime_provenance.py").read_text()\n        self.assertFalse((ROOT / "senecio_polymarket/Dockerfile").exists())\n        self.assertIn("USER senex:senex", docker)\n        self.assertNotIn("chmod -R 777", docker)\n        self.assertNotIn("mv /app/oracle/predict_only.py", docker)\n        self.assertNotIn("cp /app/oracle_runtime/predict_only.py", docker)\n        self.assertIn("from oracle_runtime.predict_only import", runner)\n        self.assertNotIn("from predict_only import", runner)\n        self.assertIn('root / "Dockerfile"', provenance)\n        self.assertNotIn('root / "senecio_polymarket" / "Dockerfile"', provenance)\n\n    def test_f4_edge_dashboard_route_parity_and_bounded_polling(self):\n        edge = (ROOT / "edge/order070/worker.js").read_text()\n        js = (ROOT / "senecio_polymarket/frontend/app.js").read_text()\n        self.assertIn('"/api/oracle/predictions/db"', edge)\n        self.assertIn("/api/oracle/predictions/db?limit=50&symbol=BTCUSDT", js)\n        self.assertIn("setInterval(refreshPredictions, 60000)", js)\n        self.assertNotIn("setInterval(refreshOracle, 10000)", js)\n\n    def test_f5_artifact_name_uses_checked_out_exact_head(self):\n        wf = (ROOT / ".github/workflows/senex-order-070.yml").read_text()\n        self.assertIn("name: order070-sealed-${{ steps.exact_identity.outputs.head }}", wf)\n        self.assertNotIn("name: order070-sealed-${{ github.sha }}", wf)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''',
)

# Syntax/semantic static gates before any workflow test or commit.
for py in [
    "senecio_polymarket/backend/main_real.py",
    "senecio_polymarket/backend/authority_snapshot.py",
    "senecio_polymarket/backend/oracle_runner.py",
    "senecio_polymarket/backend/runtime_provenance.py",
    "senecio_polymarket/oracle_runtime/predict_only.py",
    "senecio_polymarket/tests/test_order_070_r6_public_boundary.py",
]:
    ast.parse(read(py), filename=py)

# Frozen predictor/model source is deliberately untouched.
assert read("senecio_polymarket/oracle/predict_only.py")
print("ORDER070_R6_CONSOLIDATED_PATCH=APPLIED")
print("F1_PUBLIC_POSITIVE_ALLOWLIST=PATCHED")
print("F2_AUTHORITY_SINGLE_WRITER_BOUNDED=PATCHED")
print("F3_CANONICAL_ROOT_BUILD_NONROOT=PATCHED")
print("F4_EDGE_DASHBOARD_PARITY=PATCHED")
print("F5_EXACT_ARTIFACT_IDENTITY=PATCHED")
