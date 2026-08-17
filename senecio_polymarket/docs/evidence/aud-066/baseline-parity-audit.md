# AUD-066-R1 Baseline Parity Audit

Authority: Issue #23 comment `5311482504`.

## Exact base inspected

- `BASE_MAIN_SHA=43c8023d3a4623381e45da02d9efa8e9b5888f47`
- `BASE_MAIN_TREE=20ec5775ea37a7288e8cd8748ea304843d9b0866`
- `institutional_core.py` base blob: `2e2be8204264184ab22a3801c64430ed5a1c57cf`
- `exchange_connector.py` base blob: `5c7c0f73de65e837ee3ca9e13da9e1f8ee3a2530`

## Exact SENEX semantics that matter for parity

The base decision core does not consume the AUD-066 Arm A proxy directly. Its base-SHA path includes stateful and transformed inputs including:

- price momentum and volume delta from the production OHLCV cadence;
- a stateful five-observation bid/ask imbalance smoothing buffer;
- `orderflow = smoothed_bidask * (1 + abs(volume_delta))`;
- `funding_signal = -funding_rate * 100`;
- OI momentum derived from `oi_change_24h_pct`;
- spread-derived liquidity quality and production volatility transformation;
- frozen weighted directional pressure inside `SingleDecisionCore.compress_features`;
- production regime/suppression state outside the isolated AUD-066 logistic proxy.

The base connector's depth calculation requires the full bid/ask level arrays and aggregates notional depth within 0.5% of mid. AUD-066's zero-cost Tardis `quotes` files contain top-of-book bid/ask price and amount, not the full L2 ladder required to reproduce that production depth transform exactly.

## Missing parity inputs under the current $0 public-data contract

`BASELINE_PARITY=NOT_ACHIEVABLE_AT_ZERO_COST` for this evidence package because exact reconstruction would require inputs/state not present in the frozen AUD-066 dataset contract:

1. `FULL_L2_ORDERBOOK_DEPTH_WITHIN_0_5_PERCENT` — absent from the Tardis top-of-book `quotes` source used by AUD-066.
2. `CONTIGUOUS_PRECEDING_24H_OPEN_INTEREST_HISTORY` — disjoint first-of-month day samples do not provide the exact preceding 24-hour history needed to reproduce production `oi_change_24h_pct` at every decision timestamp.
3. `EXACT_PRODUCTION_INGEST_CYCLE_SEQUENCE` — the stateful EMA-5 bid/ask buffer evolves per production ingest cycle; imposing a synthetic replay update cadence would be an assumption, not parity proof.
4. `FULL_PRODUCTION_REGIME_SUPPRESSION_STATE` — the isolated research dataset does not persist all production runtime state/history needed to reproduce those gates point-in-time.

## Consequence

Arm A remains a documented research proxy. Its proper-score deltas are valid only as `PROXY_ONLY` evidence. They cannot establish `NET_NEW_VALUE=NO` or `REALIZED_LIQUIDATION_VALUE=NO` versus current SENEX.

R1 therefore fails closed:

- `REALIZED_LIQUIDATION_VALUE=INCONCLUSIVE`
- `NET_NEW_VALUE=INCONCLUSIVE`

No replacement proxy is silently promoted to SENEX parity. No production code, weights, thresholds, runtime state, Supabase data, Northflank configuration, or RUNTIME017 surface is changed.
