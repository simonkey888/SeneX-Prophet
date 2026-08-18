# AUD-067-R1 / ORDER-WR-003 canonical worker contract

This surface is isolated from the SENEX oracle/production runtime. It is a zero-spend defensive ATM worker candidate only. `READY_FOR_ATM_INTEGRATION` is reserved for independent AUD.

## F001 — canonical ATM boundary

External protocol is `atm-worker.v1`; `worker_id=senex-prophet`; `worker_version=aud067.r1`. Every request binds `job_id`, `canonical_opportunity_id`, `worker_id`, `work_lease_id`, `attempt`, `scope_hash`, target/snapshot identity, `allowed_paths`, `required_capabilities`, `structured_requirements`, frozen acceptance, expected deliverable, deterministic checks, provenance, temporal `as_of/cutoff` when applicable, active lease/deadline and `max_spend_usd=0`. Canonical identity/scope mismatch rejects before ACK.

Completion echoes canonical identity, protocol/source SHA, verified snapshot, artifacts/hashes, task result, non-authoritative worker assessment, timestamps and zero-spend side effects. The worker cannot claim paid/profitable/external accepted/economic truth.

## F002 — exact snapshot and workspace isolation

`scope_hash` covers `target_snapshot_manifest`. Dataset manifests bind snapshot id + per-file SHA-256 + byte count. Git subset manifests additionally bind exact base commit + Git blob SHA for every admitted file. Actual bytes are checked before ACK and re-recorded in completion provenance. Target/state must be distinct roots inside a declared isolated job workspace and cannot overlap canonical SENEX, RUNTIME017 or production-style mounts.

## F003/F004 — durability and bounded execution

Lifecycle is RECEIVE → VALIDATE → snapshot/workspace verify → ACK → RUNNING/PROGRESS → RESULT_READY → FINALIZE. Crash injection is proven after ACK, during work, and after artifact materialization before finalize. The third recovery reuses durable pending result/artifact hashes and does not rerun the operation. Worker deadline/cancellation checks occur at bounded read/write/loop checkpoints. Expected hostile/malformed input terminalizes deterministically.

Bounds: 1 MiB/file, 4 MiB aggregate, 10k JSON rows, bounded JSON depth/list/object/string complexity, strict UTF-8. Target code is AST-parsed only and never imported/executed.

## F005/F006 — monotonic result semantics and independent acceptance

A semantic local `FAIL` can never map to top-level `SUCCEEDED`; it terminalizes `FAILED`. `UNKNOWN`/`INCONCLUSIVE` are not promoted to PASS claims. Worker assessment is explicitly `authoritative=false` and local-evidence-only. `external_acceptance.status=NOT_EVALUATED_BY_WORKER` always. A separate `independent_check_completion()` re-hashes exact artifacts and evaluates frozen static predicates without trusting worker self-assessment; the required favorable-local/tampered-artifact negative case is tested and rejected.

## F007 — temporal/statistical truth

Temporal jobs require explicit frozen `as_of` and `cutoff`. Availability/receipt time is distinct from event time; future availability fails. Missing prior history remains missing and downgrades to `UNKNOWN`; incomplete provenance cannot become authoritative. Statistical minimum evidence is frozen before evaluation; `n < min_n` is `INCONCLUSIVE`, non-finite/malformed values fail. Robustness thresholds require an explicit training prefix; full-sample medians are never labeled train-only.

## F008 — exact public shadow

The CAN_HANDLE shadow uses actual bytes of `tableau/server-client-python` at commit `aa9e3a0bd3114e0dbb7ec41abd4784483fb89277`, path `tableauserverclient/models/user_item.py`, Git blob `0ba1e8eb2ec094471b11b579094555c8275144bc`, tied to public issue #1809. CI performs one explicit bounded public download, verifies the Git blob, freezes SHA-256/size/blob into the job, and the network-deny worker independently re-verifies the bytes before ACK. The repair shadow is intentionally bounded to two exact issue anchors and is not a claim of a complete upstream fix. No outreach/upstream mutation occurs. `learning-snake#24` remains `NEEDS_OTHER_WORKER`.

## F009 — PROVEN_ONLY

Advertised capabilities are only: deterministic replay; causal cutoff validation; provenance/hash validation; statistical evaluation; robustness/regime stress; bounded Python/data-pipeline repair. `regression_case_generation` was removed because it lacked separate E2E proof. Exact-head CI materializes ORDER-WR-003-equivalent remote truth, contract, PROVEN_ONLY capability matrix, security/temporal policies, negative tests, all crash points, real-world shadow, oracle preservation, test results, CI receipt, integration notes, deterministic SHA-256 manifest and secret scan.

## Hard locks

`PRODUCTION_MUTATION=0` · `SUPABASE_MUTATION=0` · `NORTHFLANK_MUTATION=0` · `RUNTIME017_MUTATIONS=0` · `THRESHOLD_WEIGHT_TUNING=0` · `REAL_TRADING=0` · `OUTGOING_SPEND_USD=0` · `MERGE=NO` · `DEPLOY=NO` · `RESTART=NO`.
