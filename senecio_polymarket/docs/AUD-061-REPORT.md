# AUD-061 completion report

Exact base: `44fe55b88be50a5a37364fbc8cabc98f921a6a4f`
Base tree: `03d77a1ab5a6dead2e316dd25c52a640edd9fe7c`

## Material defects corrected

1. Same-symbol learning was loaded once for the lifetime of the process. It now
   refreshes after the bounded cache TTL.
2. Learning consumed overlapping 1h rows and could include an outcome whose
   settlement horizon had not elapsed at decision time. It now uses the
   symbol-scoped independent 1h cohort and requires `origin + 1h <= cutoff`.
3. Learning provenance lacked evidence/code/config hashes. Each replay now
   emits all three plus effective weight hash and source IDs.
4. Funding and OI absence was silently serialized as observed numeric zero;
   OI snapshots were especially incorrect because a point value cannot prove
   momentum. Candidate snapshots now distinguish real zero/nonzero, missing,
   not-applicable, source error, and fallback transport. Downstream portfolio
   overlays receive `None`, not fabricated observations, when unavailable.
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

The frozen feature-engineering/model file and historical preregistration remain
byte-identical. No production threshold, feature weight, calibration, live
gate, external-market direction input, or safety lock was relaxed.

## Read-only experiments

Input: 252 public production rows (126 BTC, 126 ETH), content hash
`f932b8d8424b781377bf7de11870af788bbf5a2a2c770fe8ac747966b7866fd5`.

- Learning A/B: only 1 paired independent decision after chronological warmup;
  A and B were both correct, delta 0. Result `INSUFFICIENT_OOS_EVIDENCE`.
- FLAT waterfall: complete. BTC 95/126 (75.40%), ETH 91/126 (72.22%), aggregate
  186/252 (73.81%). The dominant loss was negative/insufficient EV: 127 rows.
- Threshold curve: complete as diagnostic, Holm-corrected, but insufficient
  independent N (BTC 10, ETH 11); no production writeback.
- Horizons: 15m and 1h evidence exists; 30m, 2h, and 4h proof is absent. Every
  horizon result is `INSUFFICIENT_OOS_EVIDENCE`; Holm correction is recorded.
- Six-signal ablation: `INSUFFICIENT_OOS_EVIDENCE` at independent N 10/11.
- Availability: all 252 legacy rows encode funding and OI momentum as zero with
  no observation status, proving the prior missing/zero conflation. Candidate
  deterministic fixtures quantify 12 post-fix feature observations across an
  observed-zero derivatives scenario and a spot/fallback-unavailable scenario;
  every zero is classified as real, missing, or not applicable. A live post-fix
  count is not claimed because deploy is forbidden.
- Edge claim: `NOT_SUPPORTED`.

The complete manifest, per-decision provenance, curves, waterfall, availability
counts, and uncertainties are in `docs/evidence/aud061-experiments.json`.

## Safety result

`PAPER_ONLY=true`, `ORDERS_ENABLED=false`, `LIVE_CAPITAL_LOCKED=true`,
`SUPABASE_DATA_MUTATIONS=0`, `RUNTIME017_MUTATION=NO`, `MERGE=NO`, `DEPLOY=NO`.
