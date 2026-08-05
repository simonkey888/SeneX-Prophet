# SENEX Paper Execution Truth

This contract applies only to public-GET, simulated paper execution. It does not authorize authenticated CLOB requests, wallets, private keys, real orders, real fills, capital movement, deployment, or profitability claims.

## Source-time contract

`PublicOrderBook` preserves source and receive time separately. Source time must come from the source payload; a receive timestamp cannot replace it. Deterministic fixtures require explicit `FIXTURE_EXPLICIT` provenance. Missing, malformed, future, stale, or excessive pair-skew timestamps fail closed.

## Fee contract

The authoritative input is public CLOB market information, including the raw `fd` schedule and `itode` flag. SENEX preserves the raw schedule and its SHA-256. Fees use `Decimal` and are classified as `TAKER` or `MAKER`.

The pinned official references are:

- Polymarket fee documentation: `C × feeRate × p × (1-p)`, five-decimal rounding and the documented minimum behavior.
- Polymarket CLOB market-info documentation exposing `fd` and `itode`.
- `Polymarket/py-clob-client-v2` release `v1.0.1`, commit `394ecc18ab9ab20b48095b0b5c5de0042bdd6bb3`, fee file blob `6f2c7c6441e7f8455a32e8c4fb1f9455e567729d`.

When those official sources conflict for a supplied market schedule, the market is rejected with `FEE_MODEL_UNVERIFIED`. SENEX never chooses the formula that improves simulated results and never defaults to zero for a fee-enabled market.

## Sequential execution

The only execution model in this phase is `TAKER_COMPLETE_SET_SEQUENTIAL_PAPER_V1`.

The first intent is selected by deterministic input order. It is simulated against the first snapshot. The second intent is evaluated only against a distinct later snapshot or a deterministic hostile fixture after a separately recorded conservative configured delay. `itode` is evidence, not an observed latency measurement. Leg imbalance, repricing, partial completion, failure, and paper-only unwind status are recorded. Atomicity and symmetric fills are never claimed or fabricated.

## Settlement and valuation

Resolution evidence is bound to condition, market, token set, winning token, payout, source hash and raw-resolution hash. Settlement is append-only, idempotent and replayable. Entry fees remain in cost basis and are not charged twice.

Unresolved positions remain `OPEN_UNMARKED`, `OPEN_MARKED` or `RESOLUTION_PENDING`. Unknown valuation produces `equity_known=false` and null PnL/equity fields. Only verified resolution evidence can produce `RESOLVED_WIN`, `RESOLVED_LOSS` and `SETTLED` records.

## Permanent invariants

```text
paper_only=true
orders_enabled=false
live_capital_locked=true
PROFITABILITY_NOT_ESTABLISHED
```
