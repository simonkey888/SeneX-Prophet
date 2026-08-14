# AUD-062 — Decision causality forensic report

## Scope and immutable base

- Order: Issue #23 comment `5298315612`; publication/resume authorization comment `5298610051`
- Exact base SHA: `49c5f0a69609c005da80e48b585e91d8582a5ac6`
- Exact base tree: `3e323bcc2795f97b29242883d3bf2a015c092ccd`
- Production lineage observed: the same SHA, Northflank build `rugged-pump-6360`
- Observation bundle: 2026-08-14T21:46:28Z, public/read-only endpoints only
- Decisions: 348 total, 174 BTCUSDT, 174 ETHUSDT, 8 after the AUD-061 deployment
- Mutations: production 0, Northflank 0, database 0, RUNTIME017 0
- Behavior/tuning: thresholds unchanged, weights unchanged, external directional use unchanged at zero
- Publication gate: recursive decompressed scan PASS; PII/scope/non-public/auth-header/credential gates PASS

The audit remains behavior-diagnostic. The candidate changes only truth/provenance instrumentation: it names the separate survivability ruin probability, makes the EV rejection reason sign/threshold coherent, persists feature/external observation timestamps, and persists the learning evidence observation epochs. It does not change a decision threshold, weight, directional signal, trade action, or production safety lock.

## Core answer

SENEX is mostly FLAT because executable `adjusted_ev` fails the dynamic minimum EV gate, but that result is not wholly supported by one coherent EV model. The production path computes a transparent core EV and then takes the minimum against a second market-anchor EV. That anchor uses different probability, volatility, entropy, slippage, and impact semantics and does not serialize its terms. It binds 317/348 rows, turns positive core base EV negative in 159 rows, and rejects 175 rows where the core survival EV alone exceeded the dynamic minimum.

This is therefore not evidence that all abstention reflects genuinely non-positive executable expectancy. It is evidence that a conservative, semantically incompatible hidden branch materially suppresses decisions. It does not prove that removing the branch would create edge: current authority is insufficient, external horizons are mismatched, and a valid full frozen-vs-learned A/B remains causally under-proven.

Every stored decision has an explicit first binding gate from the persisted pipeline; `UNKNOWN_CAUSAL_PATH=0`. Exact future-safe reconstruction is incomplete for the eight new replay snapshots because the process-local query-observation epoch is included in `source_evidence_hash` but not persisted, and several feature/external snapshots omit exact observation timestamps.

The second audit pass found two additional material truth defects beyond the original seven: 22 positive adjusted-EV rows are persisted as `negative_ev`, and the initial 81.3253% Polymarket metric is only same-market resolved-label agreement—not causal predictive accuracy or incremental value.

## Decision and abstention distribution

| Cohort | N | LONG | SHORT | FLAT | FLAT rate |
|---|---:|---:|---:|---:|---:|
| All | 348 | 47 | 48 | 253 | 72.7011% |
| BTCUSDT | 174 | 25 | 22 | 127 | 72.9885% |
| ETHUSDT | 174 | 22 | 26 | 126 | 72.4138% |
| Pre AUD-061 | 340 | 47 | 47 | 246 | 72.3529% |
| Post AUD-061 | 8 | 0 | 1 | 7 | 87.5% |

First binding FLAT causes:

| Cause | Rows |
|---|---:|
| EV_NEGATIVE | 148 |
| EV_BELOW_DYNAMIC_MIN | 22 |
| OTHER_EXPLICIT (low conviction / size) | 39 |
| REGIME_REJECT | 28 |
| NO_DIRECTIONAL_SIGNAL | 16 |

Authority score state does not directly enter the production decision equation and accounts for zero first-binding FLAT gates. Score authority remains `INSUFFICIENT_EVIDENCE` (BTC raw 18/independent 10; ETH raw 23/independent 11).

## EV formula reconciliation

All 348 stored values reproduce within `1e-6`; maximum residual is `4.2e-7`.

```text
core_raw_ev       = p_win*avg_win - (1-p_win)*avg_loss
core_base_ev      = core_raw_ev - estimated_cost
core_survival_ev  = core_base_ev * survival_discount

anchor_p_win      = sigmoid(raw_conviction)
anchor_gross_ev   = (anchor_p_win*single_candle_vol*1.2
                    -(1-anchor_p_win)*single_candle_vol*0.8)
                    * entropy_discount
anchor_market_ev  = anchor_gross_ev - 0.0002 default slippage
                    - default-depth impact

adjusted_ev       = min(core_survival_ev, anchor_market_ev)
tradeable         = adjusted_ev > dynamic_min_ev
```

The bridge has no arithmetic residual, but the second formula is an observability and model-coherence defect. It is not the “same formula” described by the helper comment. It uses single-candle volatility rather than the core ATR, applies entropy to EV despite the core’s size-only statement, uses `sigmoid(conviction)` rather than the directional pressure probability, and subtracts fixed/default execution terms without the current order-book depth.

The cost model does not identify one executable instrument: ticker/OHLCV/order book are OKX spot, funding/OI are OKX USDT swap, commission is an unsupported maker constant, and the anchor uses fixed slippage/default depth. No cost parameter was changed in this audit.

## Risk and survivability

All 348 rows persist:

```text
step3_risk.ruin_prob = 0.0
step3_risk.surv_reason = HIGH_RUIN_PROB: 50.00% > 30% threshold
```

These are distinct models: the first is the fresh oracle risk state; the second is the survivability module’s insufficient-data prior. The payload does not serialize `survivability_ruin_prob`, so the human reason has no matching machine field. Risk affects core EV and size; survivability affects size only. The current state does not prove core ruin risk of 50%.

The candidate now serializes `step3_risk.survivability_ruin_prob` from the same survivability result that generates `surv_reason`. Historical rows remain unchanged and explicitly classified as contradictory telemetry.

## Feature truth and causality

- Post-AUD-061 missing funding/OI observations retain `MISSING` and are excluded from the agreement denominator.
- A first OI observation cannot manufacture momentum; later comparable observations produce real non-zero momentum.
- Legacy numeric zero remains `UNKNOWN_LEGACY_ZERO_CONFLATED`, not observed neutral evidence.
- Funding/OI carry explicit OKX swap instrument identity and timestamps when available.
- Orderflow, volume delta, bid/ask imbalance, and price momentum still lack per-feature acquisition timestamps in the persisted replay payload. Exact `timestamp <= decision cutoff` proof is therefore incomplete for those fields.

The candidate attaches last-candle timestamps to OHLCV-derived features, source order-book timestamps to book-derived features, and the latest of both to combined orderflow. No value or availability status is changed.

No feature counterfactual invents an unknown missing value. Material impact of a missing true value is labeled `NOT_ESTIMABLE_UNKNOWN_TRUE_VALUES`.

## Learning frozen-vs-learned

Eight post-deploy decisions have `decision_replay_v1`. Using their stored source IDs and assigning the decision cutoff as the latest possible query observation:

- effective weight hash matches: 8/8
- learned final decision matches production: 8/8
- frozen-vs-learned decision changes: 0/8
- pressures and EV change in all relevant pairs
- persisted source evidence hash matches: 0/8

The exact process-local query observation epoch is hashed but not serialized. The result is correctly labeled `INSUFFICIENT_CAUSAL_PROVENANCE` and `COMPONENT_LEVEL_WEIGHT_SENSITIVITY_NOT_MODEL_AB`, not a full model A/B. The same N=10/11 cohort both mutates weights and feeds size calibration while reporting authority remains insufficient; this has no prospective statistical justification in the repository.

For future rows, the candidate persists `learning_source_settlement_observation_epochs` alongside source IDs and the evidence hash. Existing eight rows still fail closed; no observation time is invented for them.

## Confidence semantics

- `confidence` / conviction: raw uncalibrated conviction
- `up_prob` / `down_prob`: heuristic `sigmoid(total_pressure*5)` transforms, not P(correct)
- Polymarket/Kalshi probabilities: market-implied prices, not SENEX-calibrated probability
- observed win rate: empirical diagnostic only
- authoritative calibrated probability: unavailable

A value such as `96% UP` from `up_prob` is not a calibrated probability. The probability-like name remains a material semantic risk even though the dashboard labels persisted `confidence` as raw conviction.

## External shadow ledger and horizons

The ledger contains 167 decision-time Polymarket observations; Gamma public resolution is available for 166. External directional application is zero for every row.

| Metric | Result |
|---|---:|
| Exact SENEX 1h / Polymarket 5m horizon matches | 0% |
| Internal/external directional agreement | 62.4161% |
| Strong external observations | 119 |
| Polymarket same-market direction vs its own 5m settlement | 81.3253% |
| Direction disagreements (“would flip” diagnostically) | 56 |

The 81.3% figure is a same-market descriptive agreement sampled during its five-minute window, not SENEX 1h accuracy or deployable edge. Exact snapshot timestamps were not persisted; freshness permits only a non-authoritative inference. `BLENDED_SHADOW` is therefore not computed without a preregistered blend. Kalshi contributes 167 shadow rows but has a 15-minute target mismatch; Boros contributes 334 context rows but has a funding/product mismatch. All three ledgers persist `external_applied=0`. External incremental value, redundancy, and abstention benefit remain `NOT_ESTIMABLE_NO_CAUSALLY_ALIGNED_OOS_LABEL`.

## Authority feedback loop

`INSUFFICIENT_EVIDENCE` controls reporting/readiness, not production direction. Authority N or win rate does not directly force FLAT and there is no demonstrated “poor score → lower current EV → more FLAT” loop. A distinct learning feedback exists: independent proof rows begin bounded weight mutation at N=10 and also populate size calibration. It changes pressure/EV in the reconstructible sample but changed no final decision in the eight paired rows.

## Governance / automatic CD

`main` is `protected=false`, repository rulesets are empty, and no required checks are enforced. Northflank has a successful deployment/status context for exact main SHA and follows main automatically. In addition, `.github/workflows/oracle.yml` is manually dispatchable, requests `contents: write`, checks out main, commits generated predictions, and runs `git push`.

```text
AUTO_CD_SAFE_UNDER_CURRENT_GOVERNANCE=NO
```

Direct push, force push unless separately actor-restricted, GitHub UI edit, the contents-write oracle workflow, app/bot write, merge without checks, stale approved PR merge, and unaudited merge can cross the repository-write-to-production boundary.

Minimum guardrails: protect main; require PR plus owner/AUD approval; require exact-head SCORE-001, SCORE-002, smoke, and audit checks; block force pushes/deletions; eliminate standing bypass; downgrade/remove the contents-write direct-push workflow; restrict write-capable apps; and require a deployment approval bound to the reviewed SHA.

No settings were changed by AUD-062.

## Material findings

| ID | Severity | Finding |
|---|---|---|
| AUD062-F001 | HIGH | Hidden/incompatible market-EV branch materially suppresses decisions |
| AUD062-F002 | MEDIUM | Risk machine field and human ruin reason expose contradictory unlabeled semantics |
| AUD062-F003 | HIGH | Unprotected main + auto-CD creates unaudited production paths |
| AUD062-F004 | MEDIUM | Replay hash and feature/external snapshot provenance are not independently reconstructible |
| AUD062-F005 | MEDIUM | Cost model lacks one executable instrument and primary parameter provenance |
| AUD062-F006 | MEDIUM | `up_prob/down_prob` imply probability despite heuristic uncalibrated semantics |
| AUD062-F007 | MEDIUM | N=10/11 evidence mutates weights and also feeds size calibration before score authority |
| AUD062-F008 | MEDIUM | `negative_ev` mislabels 22 positive-EV dynamic-threshold failures |
| AUD062-F009 | MEDIUM | 5m same-market settlement agreement is not causal accuracy or value-add evidence |

No critical finding was established. All nine material findings include the full required finding schema in `docs/evidence/aud-062-findings.json` (2 HIGH, 7 MEDIUM).

## Publication sanitization and provenance

The publication bundle is allowlist-minimized. Redundant `created_at`/`ev`, unrelated enriched metadata, unnecessary market-response fields, and unused identifiers were removed while preserving all fields consumed by deterministic replay. Every row-level dataset carries source class, capture time, endpoint/class, raw/derived status, transformation, row count, and canonical SHA-256; the CSV carries the same block as comment headers.

The exact publication set—including decompressed gzip contents—was scanned with `aud062-deterministic-pattern-scan/AUD-062-publication-scan-v1` and `detect-secrets/1.5.0` (network verification disabled). There were 70 entropy candidates: 66 reviewed content/Git-object digests and 4 exact scanner/workflow-syntax false positives; confirmed secrets: 0. Required result:

```text
PUBLICATION_SECRET_SCAN=PASS
PUBLICATION_PII_REVIEW=PASS
PUBLICATION_SCOPE_REVIEW=PASS
PUBLICATION_NONPUBLIC_DATA=NONE
PUBLICATION_AUTH_HEADERS=NONE
PUBLICATION_CREDENTIALS=NONE
```

## Hypothesis disposition

| Claim | Result |
|---|---|
| A — FLAT primarily caused by adjusted EV below dynamic minimum | CONFIRMED |
| B — positive base EV can become negative adjusted EV and FLAT | CONFIRMED |
| C — ruin numeric/reason contradiction | CONFIRMED as persisted semantic contradiction |
| D — Polymarket observed but applied at zero | CONFIRMED |
| E — restart OI missing is masked, not fabricated zero | CONFIRMED |
| F — effective weights mutate while authority remains small | CONFIRMED |
| G — external disagreement may be horizon mismatch | PARTIALLY_CONFIRMED; causal explanation not estimable |

## Reproduction and gates

```bash
cd senecio_polymarket
python -m scripts.run_aud062 analyze \
  --input docs/evidence/aud062-public-inputs.json.gz \
  --output docs/evidence
python -m unittest tests.test_aud_062 -v
python -m unittest discover -s tests -v
python -m compileall -q .
```

The AUD-062 test workflow generates the full sanitized/scanned artifacts twice and requires byte-identical hashes, non-zero test discovery, zero confirmed secret candidates, exact base ancestry, no threshold/weight or directional activation diff, decision-semantics invariance for the instrumentation corrections, PAPER/live locks, external applied=0, and RUNTIME017 absence from the diff.
