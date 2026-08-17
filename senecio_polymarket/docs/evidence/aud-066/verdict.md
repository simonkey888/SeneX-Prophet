# AUD-066-R1 Corrected Verdict

Terminal question: `DOES_F_LIQUIDATION_PRESSURE_V1_ADD_INCREMENTAL_POINT_IN_TIME_OOS_VALUE_OVER_CURRENT_SENEX_BASELINE?`

- BASELINE_PARITY: **NOT_ACHIEVABLE_AT_ZERO_COST**
- NET_NEW_VALUE: **INCONCLUSIVE**
- REALIZED_LIQUIDATION_VALUE: **INCONCLUSIVE**
- ROBUSTNESS_MATRIX: **FRAGILE**
- ESTIMATED_CLUSTER_VALUE: **NOT_TESTABLE_AT_ZERO_COST**
- PAID_HEATMAP_DEPENDENCY: **REJECT**
- BEST_CANDIDATE_SPEC: **NONE**
- PRODUCTION_INTEGRATION_RECOMMENDED_FOR_SEPARATE_ORDER: **NO**

## R1 correction

Arm A is a research proxy, not an exact replay of current SENEX. The exact parity inputs missing under the frozen $0 dataset contract are enumerated in `baseline-parity-audit.md` and `data-manifest.json`. Therefore the proxy result cannot support a terminal SENEX-level `NO`.

The parent proxy experiment previously mapped its non-positive result to `NO`. R1 corrects the inference contract: no equivalence/no-value margin was predeclared before terminal blocks were observed, and the reported proper-score confidence intervals cross zero. Absence of proof of improvement is therefore **not** proof of no incremental value.

The mandatory robustness families are materialized in `robustness-matrix.json` under predeclared train-only thresholds. Any robustness finding remains proxy-only unless baseline parity is independently proven.

## Preserved findings

- Estimated liquidation clusters remain semantically distinct from realized force-order flow and are not testable under the present $0 source gate.
- Paid heatmap dependency remains rejected for this order.
- Production integration remains not recommended because sufficient positive evidence is absent; this is not a zero-value claim.

No production integration, merge, deploy, restart, tuning, Supabase write, Northflank mutation, external directional activation, real trading, or RUNTIME017 mutation occurs in AUD-066-R1.
