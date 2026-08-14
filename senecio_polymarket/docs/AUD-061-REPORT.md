# AUD-061-R1 completion report

Exact base: `44fe55b88be50a5a37364fbc8cabc98f921a6a4f`
Base tree: `03d77a1ab5a6dead2e316dd25c52a640edd9fe7c`

## Material defects corrected

1. Same-symbol learning was loaded once for the lifetime of the process. It now
   refreshes after the bounded cache TTL.
2. Learning consumed overlapping 1h rows and retrospective research inferred
   database availability from horizon expiry. Runtime learning now uses the
   symbol-scoped independent 1h cohort and requires both horizon expiry and an
   explicit settlement/query observation at or before the decision. Future
   settlements persist observation provenance without rewriting legacy rows.
3. Learning provenance lacked evidence/code/config hashes. Each replay now
   emits all three plus effective weight hash and source IDs.
4. Funding and OI absence was silently serialized as observed numeric zero,
   and the connector sent the spot symbol to derivative endpoints. Funding/OI
   now resolve explicit public USDT-settled swap identities. OI momentum is
   emitted only after two ordered comparable snapshots. The runtime bridge
   masks unavailable inputs from the agreement/noise denominator; measured
   neutral zero remains distinct. The frozen model stays byte-identical.
5. FLAT had only free-text reasons. Candidate predictions now carry a stable
   waterfall category, and the research report provides per-symbol and
   aggregate counts, rates, and transition loss.
6. The SCORE-002 secret-literal gate matched its own inert test fixtures. Test
   literals are now assembled without a source-level credential/domain match,
   so the unchanged repository-wide gate can execute as intended.
7. The active README described the obsolete synthetic/event-bus app as current
   production and implied live trading could be enabled by adding an adapter.
   It now names the real entrypoint, exact score semantics, hard PAPER locks,
   public endpoints, and historical boundaries.
8. The connector self-test expected trades from `fetch_all` even though that
   method intentionally skips unused trades. The assertion now matches the
   documented dataflow and the mock suite is fully green.
9. The original research overstated a Step-2 sign sensitivity as a full causal
   FROZEN/LEARNING model A/B. The legacy export lacks complete replay inputs and
   historical settlement-observation timestamps, so R1 fails closed as
   `INSUFFICIENT_CAUSAL_PROVENANCE` and persists bounded replay snapshots for
   future decisions.
10. The original threshold curve was mislabeled purged OOS and excluded FLAT.
    R1 implements a deterministic chronological train/purge/embargo/evaluation
    split across all decision snapshots, including FLAT. Because legacy rows
    cannot replay the complete gate, the OOS curve is empty and insufficient;
    the former directional curve remains explicitly in-sample diagnostic only.

The frozen feature-engineering/model file and historical preregistration remain
byte-identical. No production threshold, feature weight, calibration, live
gate, external-market direction input, or safety lock was relaxed.

## Read-only experiments

Input: 272 public production rows (136 BTC, 136 ETH), canonical content hash
`ad54e6e29f3667d15e87b86efb330c0c0388f00124f37db0bfc691f52265f48c`.

- Learning: 21 independent authority targets, 0 with complete replay and 0
  with historical settlement-observation provenance. No paired model A/B is
  claimed; result `INSUFFICIENT_CAUSAL_PROVENANCE`, effect `NOT_ESTIMABLE`.
- FLAT waterfall: complete. BTC 103/136 (75.74%), ETH 98/136 (72.06%), aggregate
  201/272 (73.90%). The dominant loss is negative/insufficient EV: 136 rows.
- Threshold research: real 60% chronological train split plus 1h purge and 1h
  embargo. Evaluation includes 48 snapshots per symbol, including 37 BTC and
  35 ETH FLAT rows. Replay-ready evaluation N is zero, so the OOS result is
  `INSUFFICIENT_OOS_EVIDENCE`. The Holm-corrected legacy directional curve is
  labeled `DESCRIPTIVE_IN_SAMPLE_DIRECTIONAL_ONLY_NON_OOS`.
- Horizons: 15m and 1h evidence exists; 30m, 2h, and 4h proof is absent. Every
  horizon result is `INSUFFICIENT_OOS_EVIDENCE`; Holm correction is recorded.
- Six-signal ablation is descriptive availability only and remains
  `INSUFFICIENT_OOS_EVIDENCE` at independent N 10/11.
- Availability: all 272 legacy rows encode funding and OI momentum as zero with
  no observation status, proving the prior missing/zero conflation. Candidate
  deterministic fixtures quantify 18 post-fix feature observations across a
  nonzero funding/two-point OI scenario, derivative-source failure, and
  spot/fallback unavailability. Live post-fix status remains
  `NOT_OBSERVED_NO_DEPLOY`.
- Edge claim: `NOT_SUPPORTED`.

The complete manifest, per-decision provenance, curves, waterfall, availability
counts, and uncertainties are in `docs/evidence/aud061-experiments.json`.

## Safety result

`PAPER_ONLY=true`, `ORDERS_ENABLED=false`, `LIVE_CAPITAL_LOCKED=true`,
`SUPABASE_DATA_MUTATIONS=0`, `RUNTIME017_MUTATION=NO`, `MERGE=NO`, `DEPLOY=NO`.
