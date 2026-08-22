# ORDER-070 runtime truth contract

The production public process is `backend.main_real:app`. The public surface is a positive allowlist: root/static plus the explicitly enumerated observational APIs only. Unknown legacy GETs, legacy WebSocket inheritance, research, antifragility, observability, metrics, admin and every unsafe HTTP method are not mounted.

Authority is single-writer. A background lifecycle task performs complete BTCUSDT authority-history + exact-count captures at a bounded cadence of at least 300 seconds. Public score/state/live-gate/snapshot/market-context/prediction-feed GETs only observe the immutable cached generation and fail closed when no valid fresh generation exists; public GETs cannot trigger Supabase authority reads or rotate generation state.

`/api/oracle/predictions/db?limit=50&symbol=BTCUSDT` is a bounded read-only view of rows already captured by the authority lifecycle. It performs no request-time Supabase query. The dashboard polls this evidence no faster than once per 60 seconds.

The repository-root `Dockerfile` is the sole canonical production image definition. `senecio_polymarket/Dockerfile` is retired. The image imports the runtime bridge explicitly through `oracle_runtime.predict_only`; the frozen predictor remains at `oracle/predict_only.py` and is never moved/copied over at build time. The runtime is non-root and only `/app/data` plus `/app/oracle/senecio_output` receive narrow owner write permission; no app path is world-writable.

Build provenance binds the exact source commit, source tree, OCI digest and a canonical build digest computed from the root Dockerfile, locked dependencies and launcher. No prediction thresholds, model weights or signal-generation rules are changed by ORDER-070.

The temporary Cloudflare Worker under `edge/order070/` is proof infrastructure only. It strips authorization/cookie headers, proxies only its explicit GET/HEAD allowlist (including the bounded BTC prediction view required by the dashboard), and denies unknown paths and unsafe methods before origin.

Permanent runtime locks remain `trade_mode=PAPER`, `orders_enabled=false`, `live_capital_locked=true`.
