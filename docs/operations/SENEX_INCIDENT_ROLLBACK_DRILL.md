# SENEX incident and rollback drill

## Drill objective

Demonstrate that a paper-only production fault can be detected, isolated and rolled back without data loss, secret disclosure, order capability or real-capital action.

## Scenarios

1. **Deployment SHA mismatch:** public state does not equal the approved delivery SHA.
2. **Persistent volume absent or remounted:** expected raw-chain path is missing or has a different volume identity.
3. **Integrity/replay failure:** file hash, chain replay or recovery journal diverges.
4. **Safety drift:** any safety flag differs from `true/false/true` for paper-only/orders/live-capital-lock.
5. **Crash between durable boundaries:** restart must recover exactly once without duplicate first- or second-leg fills.

## Procedure

- Freeze further promotion and retain all evidence.
- Record current deployment/build, source SHA, volume identity, backup identity and endpoint responses.
- Stop or replace only the affected paper service; never touch wallets or trading endpoints.
- Restore the verified pre-deployment backup to the authorized rollback target.
- Redeploy the recorded rollback SHA/build.
- Validate endpoints from two clients and require integrity/replay equality.
- Compare pre-incident, fault and restored manifests and hashes.
- Publish the drill result with timestamps, IDs, hashes and any unresolved uncertainty.

## Pass criteria

The drill passes only when the restored service reports the rollback SHA, safety invariants remain intact, the persistent corpus matches the verified backup, integrity and replay pass, and no real order or capital action occurred. Without authenticated infrastructure access, the drill status is `NOT_EXECUTED`, not PASS.
