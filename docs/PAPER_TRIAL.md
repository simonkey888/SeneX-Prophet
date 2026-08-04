# SENEX paper trial contract

The trial runs for a target of 60 minutes or at least 12 completed BTC five-minute windows using public Polymarket GET endpoints. It stores request provenance, exact public response bodies in a hash chain, deterministic paper decisions, simulated intents/fills, portfolio journal/snapshots, risk decisions, abstentions, summary, and `SHA256SUMS`.

A zero-fill session is valid when abstentions are explicit. Fixture integration proves the simulated execution path independently. No profitability claim is permitted.

Execution fallback is GitHub Actions when an isolated Northflank service cannot be created without touching existing production.
