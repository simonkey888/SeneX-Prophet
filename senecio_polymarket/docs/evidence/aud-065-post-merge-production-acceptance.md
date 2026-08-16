# AUD-065 post-merge/deploy production acceptance procedure

Do not execute without separate OWNER merge/deploy/restart authorization.

1. Resolve the final merged `main` SHA and tree and require the Northflank build/deployment to be pinned to that exact SHA; reject `latest` or any moving ref.
2. Verify runtime health, `cycles_failed=0`, `last_error=null`, and PAPER/live/order locks.
3. Read real Supabase per-symbol source totals with complete keyset pagination, never a newest-N authority query.
4. For BTCUSDT and ETHUSDT independently, reconcile source row count, canonical proof-qualified raw N, `INDEPENDENT_NONOVERLAP_1H` N, wins/losses, WR, Wilson/gates and score reasons against `/api/oracle/score?symbol=...`.
5. When either symbol exceeds 500 source rows, prove the API `input_rows/total_predictions` equals the complete source count and that an old valid proof row remains represented in authority after newer same-symbol rows are appended.
6. Reconcile runtime `directional_stats.per_symbol` and portfolio/research live-gate diagnostics to the same authority cohort.
7. Verify dashboard total input rows, raw proof N and independent authority N are derived from the reconciled score API and remain separately labeled.
8. Verify latest persisted decisions remain `learning_mutation_authority=SHADOW_ONLY`, `production_learning_mutation_enabled=false`, `mutations=0`, `uses_only_prior_settled_evidence=true`, and base/decision/effective weight hashes are identical.
9. Verify Polymarket/Kalshi/Boros are read-only with zero effective external directional contribution, no RUNTIME017 mutation, no data rewrite/backfill, no real trading and no wallet/private-key/order/capital path.
10. Materialize exact-SHA logs/artifacts and fail closed on any incomplete authority retrieval.
