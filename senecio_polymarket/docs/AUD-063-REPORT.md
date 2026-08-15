# AUD-063 — Settlement starvation, authority and causal-learning remediation

Generated: 2026-08-15T03:15:00+00:00

## Independent reproduction

The exact `main` implementation at `49c5f0a69609c005da80e48b585e91d8582a5ac6` was executed against an in-memory PostgREST boundary before the fix. Run `31860917485` reproduced the defect: two consecutive bounded selector calls returned 100/100 `FLAT`, no server-side direction predicate was present, and the later directional fixture was never returned. The separate public read-only baseline observed 388 visible rows, 347 `outcome=NULL`, 57 directional NULL rows older than one hour, and 104 older FLAT NULL rows ahead of the oldest eligible directional row.

## Remediation

- Directional selection is server-side, oldest-first and keyset-paginated by `(ts,id)`. Failed rows do not block later rows and become retryable after pass reset.
- Both 15m and 1h prices must come from a one-minute candle containing the exact target on the same public exchange as the origin witness. No current-price or unsupported-source fallback is authoritative.
- NULL settlement is one CAS writer; HTTP success without a returned changed row is a no-op. Reconciler remains repair-only for already-settled rows.
- Authority now requires `aud063-v1` historical price evidence. Legacy rows lacking it remain RAW/UNVERIFIED.
- Settlement availability is the actual persisted observation time. A later recovery cannot enter a replay whose decision cutoff predates that observation.

## Safety

No production/database/Northflank/GitHub-settings/RUNTIME017 mutation was performed. No merge or deploy. No threshold or weight tuning. External directional activation remains off. The 57-row observed backlog is inventory evidence only; no performance or edge claim is made.

## Residual limitations

Historical public one-minute candles can be unavailable or venue-limited; those rows fail closed and remain unresolved. Legacy rows lacking reconstructible source/candle provenance are intentionally excluded from authority. Production backlog recovery is code/simulation only under this order.
