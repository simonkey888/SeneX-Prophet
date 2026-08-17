# AUD-066 Leakage Audit

Status target: `PASS` only if all executed samples satisfy these gates.

1. **Availability clock:** `local_timestamp` (collector receipt) is authoritative for feature availability. Exchange timestamps are provenance only.
2. **Future liquidation exclusion:** every liquidation contributing to a trailing window satisfies `known_at_us <= decision_t`. Unit test injects an event whose exchange timestamp is before `t` but receipt time is after `t`; contribution must remain zero.
3. **Label separation:** the BTC 5m direction label is derived strictly after decision time. Label fields are not present in the feature mapping or feature hash.
4. **Delivery ambiguity:** missing exchange/receipt timestamps, negative lag, or lag >30 seconds fail closed.
5. **No random split:** chronological walk-forward only.
6. **Purge/embargo:** model blocks are separated by complete UTC sample days, far exceeding the 5m outcome horizon; no row from the terminal day is in training or validation.
7. **Train-only transforms:** `asinh` is fixed a priori; mean/std are fit on training rows only. Logistic weights are train-only. Validation chooses E before terminal block. Terminal labels do not choose features, transforms, or model settings.
8. **Estimated-cluster isolation:** the realized-liquidation feature builder contains no cluster fields. D is not mixed into B/C.
9. **Baseline collision:** OI, volume, spread, depth imbalance, funding and momentum are baseline covariates, not credited as liquidation novelty.
10. **Vendor model outputs:** CoinGlass/CoinAnk/TradingView heatmaps are not used as labels, features, or validation targets.

## Explicit negative controls

- Shifting a realized event to receipt time `t+1` must remove it from `t` features.
- Altering the future label must not alter the feature hash.
- Unknown timestamp semantics must exclude the row rather than guessing availability.

## Remaining structural limitations

Tardis is an independent normalized capture of exchange feeds, not the exchange itself. Binance's force-order stream changed to snapshot-style delivery with at most one push per second, so the dataset must not be described as complete liquidation ground truth. The study tests whether the *observable public forced-liquidation stream as captured point-in-time* adds value, not whether every liquidation at the venue is known.
