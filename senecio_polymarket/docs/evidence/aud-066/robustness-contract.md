# AUD-066-R1 Robustness Contract

Authority: Issue #23 comment `5311482504`.

This contract is materialized before the R1 terminal replay. No threshold below is selected from terminal labels.

## Regime matrix

Each terminal block is split using thresholds computed on that block's TRAIN data only:

- HIGH_VOL / LOW_VOL: train median `volatility_5m`.
- HIGH_DEPTH / LOW_DEPTH: train median top-of-book notional depth available in the AUD-066 proxy dataset.
- WIDE_SPREAD / TIGHT_SPREAD: train median `spread_pct`.
- HIGH_OI_CHANGE / LOW_OI_CHANGE: train median `abs(oi_delta_5m)`.
- LIQUIDATION_BURST / NORMAL_LIQUIDATION: train q75 `liq_burst_zscore`.

A regime cell with fewer than 30 terminal rows is `NOT_TESTABLE`; it is never imputed.

## Timing/data perturbations

The same models fitted on unperturbed training data are applied to:

- clock alignment -1 minute;
- clock alignment +1 minute;
- liquidation receipt clock replaced by exchange timestamp — diagnostic only, non-causal and never authoritative;
- deterministic 10% realized-liquidation packet removal;
- deterministic 5-minute reconnect gaps at 06:00, 12:00 and 18:00 UTC, handled fail-closed by removing decisions rather than forward-filling stale data;
- complete removal of the sole realized-liquidation source, treated as a fail-closed diagnostic rather than permission to use stale liquidation state.

## Feature-window sensitivity

Two predeclared realized-flow families are compared against the same proxy baseline:

- W1: 30s/1m realized liquidation features;
- W5: 5m realized liquidation features.

No terminal block chooses the feature window.

## Outlier sensitivity

All Arm A/B/C model features are winsorized using p01/p99 caps computed from TRAIN only, then models are refit and evaluated on terminal test rows clipped with those same train caps.

## Statistical interpretation

AUD-066 did not predeclare a smallest-effect/equivalence margin before terminal data were observed. R1 therefore does not create one post hoc.

- YES still requires the strict positive proper-score/CI gate and SENEX baseline parity.
- NO requires a valid predeclared equivalence/no-value criterion; none exists for this order.
- Intervals compatible with both benefit and harm remain INCONCLUSIVE.

## Robustness status

`FRAGILE` has precedence when a small legitimate timing/data perturbation, train-only winsorization, or the 1m-vs-5m feature-window family flips aggregate proper-score orientation.

If no such fragility is observed but one or more mandatory cells are not testable, status is `PARTIAL_NOT_TESTABLE`.

Otherwise status is `PASS`.

The robustness matrix remains evidence about the AUD-066 proxy experiment unless and until `BASELINE_PARITY=PASS`; it cannot repair missing SENEX parity by itself.
