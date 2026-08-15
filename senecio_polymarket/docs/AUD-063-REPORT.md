# AUD-063-R1 — Settlement starvation remediation hardening

Generated: 2026-08-15T04:05:00+00:00
Parent AUD-063 head: `0320f47657d4433bfc4dc3396fd0d31ffabe2270`

## Preserved AUD-063 corrections

Server-side LONG/SHORT filtering, stable `(ts,id)` keyset semantics, same-origin public historical evidence, both windows, NULL-only CAS, repair-only reconciler, actual recovery observation time, independent authority N, temporal learning cutoffs, symbol isolation, PAPER/live-capital locks, and external-directional OFF remain intact.

## R1-F001 — closed candle maturity

Historical evidence now persists `candle_close_epoch_ms` and is invalid unless `observed_at >= candle_close_epoch_ms`. The primary selector waits an additional 60 seconds beyond the one-hour horizon, and the validator independently rejects open/incomplete candles. No current ticker, nearest-candle, or alternate-venue fallback is authoritative.

## R1-F002 — restart-safe bounded fairness

The cross-cycle process-memory cursor was removed. Each verifier invocation starts from the oldest eligible directional row and performs at most two 100-row pages using local `(ts,id)` keyset seek. This is restart-safe for the first 200 eligible rows in every invocation. If the visible backlog exceeds 200, diagnostics explicitly report `RESTART_SAFE_PREFIX_ONLY_EXPLICIT_CAP`; there is no universal no-starvation claim beyond that bound. Failed rows remain retryable because scan progress is not persisted.

## R1-F003 — obsolete startup backfill quarantined

The legacy startup resettlement loop was removed. Startup performs only a synchronous zero-read/zero-write quarantine marker, then reaches the primary verifier. Existing settled-row evidence repair remains exclusively in `settlement_reconciler`, which cannot create NULL->WIN/LOSS authority.

## Safety

No production/database/Northflank/GitHub-settings/RUNTIME017 mutation. No merge or deploy. No threshold or weight tuning. External directional activation remains off. Production backlog recovery remains unexecuted and no edge claim is made.
