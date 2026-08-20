# ORDER-070 runtime truth contract

The production public process is `backend.main_real:app`. Its HTTP mutation surface is zero: only GET/HEAD/OPTIONS are accepted, and the separate `backend.admin:admin_app` is not mounted by the public launcher. Admin authentication is fail-closed when `SENEX_ADMIN_TOKEN` is absent.

Score, state, live-gate, market-context, and evidence use one symbol-scoped atomic `AuthoritySnapshot`. Exact global counts use PostgREST `count=exact`; a bounded response length is never treated as an exact total. `/healthz` is process liveness. `/readyz` is fail-closed dependency/runtime readiness.

Build provenance requires exact source commit, source tree, image digest, and canonical build digest. The canonical production image definition is `senecio_polymarket/Dockerfile`; dependency installation uses `requirements.lock` with hashes. The root Dockerfile remains a compatibility build entrypoint and uses the same locked dependency/runtime bridge contract.

Risk output distinguishes the frozen core state estimate (`state_ruin_probability`) from the survivability estimator (`survivability_ruin_probability`). The survivability reason probability is serialized from the same calculation result. No prediction thresholds, model weights, or signal-generation rules are changed by ORDER-070.

The paper instrument semantic identity is `BTC/USDT`, spot, quote USDT, horizon 1h. Because no versioned primary-source fee schedule is bound to each historical decision timestamp in repository evidence, canonical executable-paper cost authority fails closed as `COST_MODEL_NOT_AUTHORITATIVE`. The canonical EV record is diagnostic only and defines 1 bp as 0.0001 decimal return with non-overlapping fee, spread, slippage, impact, entropy, and risk semantic classes.

The temporary Cloudflare Worker under `edge/order070/` is proof infrastructure only. It strips authorization/cookie headers, proxies only allowlisted GET/HEAD paths, and rejects unsafe methods at the edge before origin. It contains no account token or credential.
