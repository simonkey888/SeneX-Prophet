# SENEX repository authority

This document describes the executable contract introduced by mission `SENEX-R0-CONTRACT-AND-GATES-001`.

The repository has four distinct authority domains: legacy Oracle on `main`, observed production on `feat/h011-v3-discovery-refresh`, the advanced candidate on `feat/h011-v3-control-plane-coverage`, and optional non-authoritative research. Branch names, green CI, PR prose and Markdown are not sufficient authority by themselves.

The mechanical source is `governance/repository_contract.yaml`, enforced by `tools/verify_repository_contract.py` and `.github/workflows/senex-repository-contract.yml`.

Permanent safety invariants are non-overridable:

- `paper_only=true`
- `orders_enabled=false`
- `live_capital_locked=true`

R0 changes no runtime, product, Docker image, deployment configuration, Northflank resource, wallet, order path or capital state. Production remains observed at `2f8503533543832147caf4c8e97a0cc6f5af3cbc` until separately verified otherwise.

The committed manifest is a critical constitutional snapshot tied to base tree `6f05c5c921ce7607dd5a39bd3cb0147e074ac451`. The complete R1 inventory remains external audit evidence and is not silently promoted into repository authority.

Production-path overrides require an exact, expiring authorization object bound to mission, base SHA, head SHA, paths and hashes. Safety invariants have no override mechanism.
