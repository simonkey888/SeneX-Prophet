# AUD-066 Security Review

Scope is isolated historical research only.

## External-source trust model

All network content is untrusted. The executable research runner allowlists only `https://datasets.tardis.dev/v1/binance-futures/...` normalized CSV sample paths assembled from fixed date/data-type/symbol constants. It does not follow user-controlled URLs, execute remote payloads, dynamically evaluate code, invoke a shell on downloaded content, or import third-party client libraries.

## Payload controls

- HTTPS only.
- 45s network timeout per request.
- `Content-Length` rejected above 180,000,000 bytes.
- Streaming byte counter enforces the same maximum even without a header.
- gzip CSV is parsed with Python standard library; rows are schema-checked and numeric fields bounded by finite-value checks.
- Only `BTCUSDT` rows are admitted to the analyzed instrument.
- Timestamp ambiguity and excessive receipt lag fail closed.
- Raw files live only in a temporary directory and are deleted when the run exits.
- SHA-256 is recorded for each accepted compressed source payload.

## Credential boundary

The dataset run requires no API key and no SENEX production credentials. It does not access Supabase, Northflank, wallets, signers, exchange accounts, private WebSockets, order endpoints, or RUNTIME017. No secrets are passed to the research process.

## Third-party code

No CoinGlass, CoinAnk, TradingView, Tardis client, exchange SDK, browser automation, Pine script, or copied third-party source code is executed. Only public data files and documentation are consumed. Python standard library is sufficient.

## Repository mutation boundary

The only allowed write-capable workflow is restricted to committing generated AUD-066 evidence files back to the explicit `research/aud-066-liquidation-pressure-e2e` branch. It asserts the branch name before any push; it never pushes `main`, never deploys, and has no production secrets. A separate exact-head verifier is read-only.

## License/terms disposition

Vendor API/website terms remain applicable. Paid/proprietary heatmap outputs are not scraped or bypassed. TradingView open-source listing is treated as documentation/reference only; its code is not copied or executed. CoinGlass and CoinAnk paid endpoints are not called. Tardis documents sample CSV first-days as downloadable without an API key; only those sample paths are requested.

Security verdict: research design is acceptable for $0 isolated evidence collection if the workflow branch assertion and payload limits pass. Any redirect to a non-allowlisted host, authentication request, oversized payload, malformed gzip/CSV, or unexpected schema must fail the affected source/day closed.
