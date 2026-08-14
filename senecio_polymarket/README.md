# SENEX ORACLE — active PAPER runtime

SENEX is a public-market observation and prediction service. The deployed
entrypoint is `backend.main_real:app`; it starts the proof/settlement oracle and
public read-only Polymarket, Kalshi, Boros, and exchange adapters. Synthetic demo
scheduling is off unless explicitly enabled for local development.

The current safety contract is unconditional:

- `trade_mode=PAPER`
- `orders_enabled=false`
- `live_capital_locked=true`
- no wallet, signer, or authenticated order path is part of this runtime

A passing statistical report does not authorize live trading. See
`docs/SENEX_CURRENT_AUTHORITY.md` for the current statistical contract and
`docs/CURRENT_OPERATIONAL_TRUTH.md` for time-bounded production observations.

## Authoritative prediction flow

1. Public exchange GETs provide OHLCV, ticker, order book, funding, and OI
   snapshots through a deterministic fallback chain.
2. The frozen `oracle/institutional_core.py` produces the six-step PAPER
   decision. The runtime overlay adds bounded, symbol-scoped learning and
   provenance without modifying the frozen model file.
3. Every persisted decision records raw conviction, model inputs, learning
   evidence, external context, and an explicit FLAT waterfall reason.
4. Settlement is proof-gated at 15m and 1h. Only the independent,
   non-overlapping 1h cohort is statistical authority; raw overlap is
   diagnostic only.
5. `authoritative_score_pct` stays null until every gate passes. `confidence`
   is raw conviction, not calibrated probability; Brier/ECE are diagnostic.

The six predictor inputs are `orderflow`, `volume_delta`,
`bidask_imbalance`, `funding_signal`, `oi_momentum`, and `price_momentum`.
Candidate decisions distinguish real observed zero/nonzero from missing,
not-applicable, source error, and fallback transport. Numeric fallback values
are never presented as observations.

## Public API

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/health` | Point-in-time liveness and runner counters |
| GET | `/api/oracle/state` | PAPER runner state and symbol-scoped diagnostics |
| GET | `/api/oracle/predictions/db?symbol=BTCUSDT` | Read-only persisted predictions |
| GET | `/api/oracle/score?symbol=BTCUSDT` | Truth-safe per-symbol 1h authority |
| GET | `/api/market-context` | Read-only real-market context and safety flags |
| GET | `/api/portfolio/live_gate` | Diagnostic gate plus mandatory PAPER lock |
| GET | `/api/final_audit/state` | Additive frozen-system audit state |

`/api/health` is a liveness endpoint, not proof of statistical edge or a
permanent availability claim. Kalshi and Boros are context-only and have no
production directional effect. Polymarket directional use remains off unless a
separate explicit PAPER experiment enables it.

## Local verification

```bash
cd senecio_polymarket
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m scripts.act_final_audit_smoke
```

Run the read-only AUD-061 research harness against exported JSON payloads:

```bash
python scripts/run_aud061.py btc.json eth.json \
  --output docs/evidence/aud061-experiments.json
```

The harness has no database client and no production writeback. It reports
insufficient evidence rather than fitting or selecting production thresholds
when the independent OOS sample is too small.

## Historical boundaries

- `GO_NOGO_CRITERIA.md` is immutable historical preregistration.
- `freeze/` and the three frozen model files remain byte-locked.
- RUNTIME017 is a separate lineage and is not part of this runtime or AUD-061.
- The legacy synthetic/event-bus modules remain in the tree for compatibility;
  they are not the default deployed data authority.
