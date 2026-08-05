# SENEX Monitoring Truth Contract

The monitoring surface is a static, read-only projection of verified SENEX artifacts. It does not calculate independent trading, PnL, readiness, data-quality, fee or settlement truth.

Required sections are `OVERVIEW`, `DATA_QUALITY`, `EXECUTION`, `PORTFOLIO_AND_SETTLEMENT`, `RISK_AND_SAFETY`, `EVIDENCE_AND_REPLAY` and `READINESS_GATES`.

Every visible metric retains its source-artifact provenance. Missing values display `UNKNOWN`, unresolved values display `PENDING`, and conflicting or unverified contracts display `UNVERIFIED`. Simulated intents and fills are always labeled `SIMULATED`. Unsettled PnL is never presented as realized. The banner always shows:

```text
PAPER ONLY
paper_only=true
orders_enabled=false
live_capital_locked=true
PROFITABILITY_NOT_ESTABLISHED
```

The site contains no form, button, wallet field, secret field, authenticated request, real-order control, mutation endpoint or fake real-time claim. This phase permits only a local preview or exact-head CI artifact; no public deployment is authorized.
