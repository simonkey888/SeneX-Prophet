# SENEX security review

## Protected assets and trust boundaries

Protected assets are immutable raw evidence, recovery journals, paper portfolio state, governance manifests, deployment identity and sanitized operational evidence. Northflank, GitHub Actions, public market-data providers and public HTTP clients are separate trust boundaries.

## Mandatory controls

- No wallet, private key, seed phrase, exchange credential or authenticated order API is permitted.
- CI and operational evidence may report secret names or presence only, never values.
- Northflank inventory starts GET-only with a scoped token.
- Mutation credentials, if later authorized, are used only after backup/restore gates and never copied into artifacts or logs.
- Source SHA, tree SHA, deployment/build ID and artifact digests are bound in every checkpoint.
- Generated fixtures are labeled as fixtures and cannot support historical-performance claims.
- Unknown integrity, replay, storage or runtime identity produces a blocked/degraded result, never PASS.

## Static exclusion scope

The paper engine, monitoring implementation and evidence builder are scanned for forbidden live-trading modules and calls. The required result is zero imports of wallet/signing clients and zero calls that create, submit, cancel or delete orders or derive API keys.

## Residual risks

Authenticated infrastructure visibility and production rollback remain unverified until a scoped Northflank token is present. Historical strategy performance remains unknown until an authoritative corpus and deterministic backtest engine exist. A 24-hour trial cannot begin before deployment and persistence are proven.
