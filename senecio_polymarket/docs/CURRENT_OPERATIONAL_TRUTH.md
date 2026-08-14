# SENEX current operational truth

Observed 2026-08-14 through public read-only HTTP endpoints. Runtime values are
a time-bounded observation, not an unconditional health promise.

- `CURRENT_MAIN_SHA`: `44fe55b88be50a5a37364fbc8cabc98f921a6a4f` at AUD-061 exact-base reconstruction.
- `CURRENT_PRODUCTION_SHA`: `UNKNOWN_NOT_EXPOSED_BY_PUBLIC_RUNTIME`.
- Current deployment ID/image digest: `UNKNOWN_NOT_EXPOSED_BY_PUBLIC_RUNTIME`.
- Last independently verified deployment provenance, historical only:
  SHA `44fe55b88be50a5a37364fbc8cabc98f921a6a4f`, deployment
  `senecio-h011-64cf779dbf`. The source boundary is the Issue #23 AUD control
  log, comments `5289793035` and `5289800503`, covering the successful PAPER
  prediction at `2026-08-14T05:19:14Z` and the next-cycle boundary at
  `2026-08-14T05:34:16.142313Z`. This is a past verification, not evidence of
  the runtime's current identity.
- Issue #4's `senecio-h011-7d4cc98cd6` and production SHA `2f850353...` remain
  older historical records and are not reasserted as current observations.
- Observed service: `h011-web--senecio-h011--wbjggn89fnf8.code.run`.
- `GET /api/health`: HTTP 200, `status=ok`, version
  `ACT-XXIX-systemic-antifragility`, 27 cycles, 0 failed, 272 DB rows at the
  final observation. This proves endpoint response at that instant only.
- `GET /api/oracle/score?symbol=BTCUSDT`: `INSUFFICIENT_EVIDENCE`, raw proof
  N=18, independent 1h N=10, authoritative score null.
- `GET /api/oracle/score?symbol=ETHUSDT`: `INSUFFICIENT_EVIDENCE`, raw proof
  N=23, independent 1h N=11, authoritative score null.
- `GET /api/portfolio/live_gate`: `unlocked=false`, `trade_mode=PAPER`,
  `effective_gate=LOCKED_BY_PAPER_POLICY`, `live_capital_locked=true`.
- `GET /api/market-context`: public Polymarket/Kalshi/Boros context is read-only.
  Kalshi and Boros have `directional_use=false`; cross-horizon/venue deltas are
  diagnostic only. Production directional use remains off.
- Confidence is raw conviction, not probability. No statistically supported
  edge claim exists. Legacy exports cannot support a complete causal model A/B
  and return `INSUFFICIENT_CAUSAL_PROVENANCE`; threshold counterfactual,
  horizon, and ablation evidence remains insufficient.
- RUNTIME017 is separate and untouched by AUD-061.
- Remaining operational blocker: public runtime does not expose a verifiable
  commit SHA, deployment ID/image digest, or Combined-service CD setting.
  Therefore Issue #4 remains open and its CD constraint is not declared solved.

Read-only response hashes are recorded in
`docs/evidence/aud061-production-observation.json`. No Supabase mutation was
performed.
