# AUD-066 Feature Specification

## Causal clock

Feature eligibility is `local_timestamp <= decision_t`. Exchange timestamp is retained for provenance and lag checks, but never overrides a later receipt time. Rows with missing timestamps, negative delivery lag, or delivery lag >30s are excluded. Label price is taken strictly after `decision_t` at the 5m horizon and never enters feature construction.

## Baseline A — exact-mechanism map to current SENEX

Current base `43c8023d...` already contains price momentum, volume delta, smoothed bid/ask depth imbalance, orderflow proxy, funding signal, OI momentum, spread/liquidity quality and volatility/regime state. AUD-066 therefore treats these as baseline rather than liquidation novelty.

| candidate component | nearest existing signal | semantic overlap | timing overlap | net-new mechanism |
|---|---|---|---|---|
| price context | price_momentum | HIGH | HIGH | NO |
| traded volume normalization | volume_delta/orderflow | HIGH | HIGH | NO |
| best-level depth | bidask_imbalance | HIGH | HIGH | NO |
| spread normalization | spread_pct/liquidity_quality | HIGH | HIGH | NO |
| OI delta | oi_momentum | HIGH | HIGH | NO |
| funding | funding_signal | HIGH | MEDIUM | NO |
| realized forced-liquidation flow | none | LOW | event-time | YES |
| estimated liquidation clusters | none exact; nearest regime/liquidity hints | LOW-MEDIUM | model/bar-time | POSSIBLY, MUST BE TESTED SEPARATELY |

## Mandatory realized features

For liquidated LONG and SHORT positions separately: USD notional in trailing 30s, 1m and 5m. USD notional for Binance USDT perpetual is normalized as `price * amount`.

Derived:
- `NET_FORCED_FLOW = short_liq_usd - long_liq_usd`; positive means forced buying from short liquidations.
- `LIQ_IMBALANCE = NET_FORCED_FLOW / total_liq_usd`.
- `LIQ_ACCELERATION = total_liq_1m(t) - total_liq_1m(t-1m)`.
- `LIQ_BURST_ZSCORE`: current 1m total liquidation USD against preceding 60 one-minute windows, all ending no later than `t`.
- `LIQ_TO_VOLUME_RATIO = total_liq_5m / traded_notional_5m`.
- `LIQ_TO_OI_RATIO = total_liq_5m / estimated OI USD`.
- `DEPTH_NORMALIZED_LIQ_PRESSURE = net_forced_flow_1m / top-of-book executable depth USD`. This is explicitly a best-level depth proxy, not a 25-level book claim.
- `SPREAD_NORMALIZED_LIQ_PRESSURE = net_forced_flow_1m / (spread_fraction * mid_price)`.
- `OI_DELTA_1M`, `OI_DELTA_5M` from last causally available derivative ticker snapshots.

All heavy-tailed continuous inputs are transformed with `asinh` and standardized using train-only mean/std.

## Arms

- **A**: current-mechanism baseline: price momentum 1m/5m, volume delta, taker imbalance, best-level bid/ask imbalance, funding, OI delta 5m, spread, short-horizon volatility.
- **B**: A + realized liquidation raw windows/imbalance/burst features.
- **C**: B + volume/OI/depth/spread-normalized liquidation pressure and OI delta 1m.
- **D**: A + estimated cluster features only if a free, replayable, timestamp-auditable cluster source is found. Under the executed $0 source gate D is `NOT_TESTABLE_AT_ZERO_COST` and cannot influence E.
- **E**: among A/B/C, choose the arm with lowest validation Brier score (tie -> simpler arm) before each terminal test block. No terminal-test tuning.

## Model and split, frozen before testing

A simple logistic regression is used solely as a controlled incremental-information probe, not as a production model proposal. Fixed parameters: 500 batch-gradient steps, learning rate 0.03, L2=0.02; no hyperparameter search. Decision grid is 5 minutes. Walk-forward uses chronological days; the last three valid days are independent terminal blocks, each with its immediately prior day as validation and all earlier days as training. There is no random row split.

Because 5m labels overlap near block boundaries, train/validation/test are separated by whole UTC sample days, which is materially larger than the 5m purge requirement. No feature selector observes validation/test labels.

## Decision rules

`REALIZED_LIQUIDATION_VALUE=YES` requires B or C to beat A on both Brier and log loss in at least two independent terminal blocks and on aggregate terminal proper scores. Otherwise it is `NO` if at least two valid blocks and adequate samples execute; otherwise `INCONCLUSIVE`.

Estimated cluster value is never inferred from realized flow. Paid heatmap value is never inferred from vendor marketing or visual examples.
