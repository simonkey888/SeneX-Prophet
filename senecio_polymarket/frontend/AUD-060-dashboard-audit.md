# AUD-060 dashboard truth audit

Base: `4e9ba38347bb9db3d0ce74266035cd979bc78778`

This inventory covers the complete dashboard. Runtime truth remains read-only; no backend authority, production wiring, database, execution, or runtime017 behavior is changed.

## Defects found and fixed

| # | Surface | Defect at exact base | Resolution | Claim class |
|---:|---|---|---|---|
| 1 | Authoritative score | `verified` was labeled “Proof-qualified” although it represented a different cohort | Show BTC input rows, raw proof-qualified N, independent 1h N, and authority N separately | API_DERIVED |
| 2 | Authoritative score | Raw observed win rate could be confused with authority | Show `authority_1h.global.win_rate_pct` as authority WR and raw observed WR as “DIAGNOSTIC ONLY” | API_DERIVED / DIAGNOSTIC |
| 3 | Authoritative score | Nullable authoritative score and score status were not explicit | Render nullable authority as an em dash and expose the API status independently | API_DERIVED |
| 4 | Score and prediction totals | BTC input scope and cross-symbol DB total were ambiguous | Label score total as BTC API scope and DB total as CROSS-SYMBOL with the displayed-window count | API_DERIVED |
| 5 | Learning loop | Learning replay N could be read as current authority N | Read replay N only from the decision snapshot and label it “not authority N” | DECISION_TIME_SNAPSHOT |
| 6 | Decision context | Historical decision inputs appeared live | Mark every decision/learning value as historical decision-time snapshot | DECISION_TIME_SNAPSHOT |
| 7 | Top bar and safety | Live lock, market mode, and runtime safety were hardcoded healthy | Derive them from the market-context API; missing fields render UNKNOWN | API_DERIVED / UNKNOWN/STALE |
| 8 | Safety defaults | Missing boolean fields collapsed to safe-looking OFF/LOCKED values | Use strict boolean evidence and fail visibly to UNKNOWN | UNKNOWN/STALE |
| 9 | Polymarket directionality | Missing runtime evidence was shown as OFF | Show UNKNOWN plus the explicit static default-off policy, not a runtime claim | STATIC_POLICY / UNKNOWN/STALE |
| 10 | Kalshi and Boros directionality | Directional use was hardcoded and venue roles were unclear | Derive when present; otherwise UNKNOWN, with both venues visibly diagnostic-only | DIAGNOSTIC / UNKNOWN/STALE |
| 11 | Source integrity | Discovery, WebSocket, scheduler, and execution health were unconditional claims | Build each row from its own API evidence and attach its visible claim class | Mixed, per row |
| 12 | Footer | Paper/runtime safety and source freshness were hardcoded | Separate static policy from API-derived execution safety and runtime-observed freshness | STATIC_POLICY / API_DERIVED / RUNTIME_OBSERVED |
| 13 | Score failures | Fetch errors were swallowed and stale score values looked current | Add score-domain ERROR/STALE state, retain last success visibly, and auto-recover | UNKNOWN/STALE |
| 14 | Prediction failures | Fetch errors were swallowed and historical tables looked current | Add predictions-domain ERROR/STALE state, retained-data overlay, last success, and recovery | UNKNOWN/STALE |
| 15 | Context failures | One connection message did not identify all affected panels or freshness | Add independent context health and stale overlays to every context-backed panel | UNKNOWN/STALE |
| 16 | Source gaps | Missing pressure, orderbook depth/transport, and freshness could look like zero/current/bootstrap | Render em dash, UNKNOWN, or STALE without inventing transport or measurements | UNKNOWN/STALE |
| 17 | BTC snapshot selection | A cross-symbol window without BTC could retain an older BTC decision | Clear decision and learning surfaces explicitly when current results contain no BTC row | UNKNOWN/STALE |

## Complete surface classification

| Dashboard surface | Primary class | Truth source / boundary |
|---|---|---|
| BTC authoritative score | API_DERIVED | `/api/oracle-score?symbol=BTCUSDT`; raw diagnostic separated from 1h authority |
| Supabase prediction window | API_DERIVED | Read-only `/api/oracle-predictions`; total explicitly cross-symbol |
| Polymarket market | RUNTIME_OBSERVED | Read-only market-context snapshot; missing evidence becomes UNKNOWN |
| Polymarket orderbook | RUNTIME_OBSERVED | Public CLOB snapshot/transport fields; missing depth is not synthesized |
| CLOB event feed | RUNTIME_OBSERVED | Public recent-event payload |
| Kalshi | DIAGNOSTIC | Cross-venue 15m context; not decision authority |
| Boros | DIAGNOSTIC | Funding context; not decision authority |
| Latest BTC decision | DECISION_TIME_SNAPSHOT | Stored prediction audit, explicitly historical |
| Runtime and safety | API_DERIVED | Market-context safety fields; missing fields become UNKNOWN |
| Learning loop | DECISION_TIME_SNAPSHOT | Stored pre-decision replay provenance, not current authority N |
| Source integrity | Mixed, visibly labeled | API evidence, static policy, diagnostics, and unknowns classified per row |
| Footer policy | STATIC_POLICY | PAPER-only UI boundary; no runtime health implied |
| Footer runtime state | API_DERIVED / RUNTIME_OBSERVED | Current API safety and source freshness, independently stale-aware |
