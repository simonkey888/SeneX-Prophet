# SENEX repository authority

R0 remains the constitutional base at `39e1cf1bdad31a2b6f2178949a2977c837ebdf18`. This mission adds a complete deterministic inventory, mechanical architecture boundaries, and an isolated paper-only trial.

Permanent non-overridable invariants:

- `paper_only=true`
- `orders_enabled=false`
- `live_capital_locked=true`
- no wallet/private-key access
- no authenticated exchange/CLOB trading
- no real order, fill, settlement, or capital movement
- no mutation of the observed production branch/service/volume/secrets

The paper runtime uses public GET data and may create only simulated intents and fills. All virtual state is replayable from an append-only journal. The observed production branch remains `feat/h011-v3-discovery-refresh` at `2f8503533543832147caf4c8e97a0cc6f5af3cbc`.
