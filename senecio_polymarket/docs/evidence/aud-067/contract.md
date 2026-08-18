# AUD-067 / ORDER-WR-003 worker contract

This surface is isolated from the SENEX production/oracle runtime. It is a zero-spend, defensive worker candidate for ATM and is not production integration.

## Identity

- `worker_id=senex-prophet`
- `worker_version=aud067.v1`
- `protocol_version=atm-worker.v1`
- source truth is an exact commit SHA; mutable branch names are never accepted as provenance authority.

## Immutable capabilities

1. deterministic data replay
2. causal time-cutoff validation
3. provenance/hash validation
4. statistical evaluation
5. robustness/regime stress testing
6. bounded Python/data-pipeline repair without executing target code
7. regression-case generation

Unknown capabilities fail closed. Network access, target-code execution, startup hooks, ambient secrets, Docker socket, SSH agent, wallets/payments, production writes, live trading and outgoing spend are not worker capabilities.

## Job binding

Every job requires protocol, job/lease/attempt identity, immutable target snapshot, allowed paths, required capabilities, structured requirements, frozen acceptance criteria, upstream issue reference, zero outgoing-spend cap, active/unexpired lease state and `fixed_job_scope_hash`. The scope hash is SHA-256 over the canonical required payload excluding only the hash field itself. Mismatch, expiry, terminal lease, unsupported authority or unsafe path rejects before work.

Every completion returns job/lease/attempt identity, worker identity/version, exact source SHA, terminal status, task result, artifacts, tests, acceptance, provenance, side effects, timestamps and the exact scope hash.

## Durable execution

ACK is atomically persisted before target work. Progress is append-only and durable. The tuple `job_id × lease_id × attempt` maps to a single state record. A repeated dispatch with the same scope returns the terminal completion; the same tuple with a different scope fails closed. Crash-injection tests prove recovery after ACK and during work. The worker never starts target child processes, so cancellation has no process tree to orphan.

## Filesystem / sandbox boundary

Only relative paths beneath `allowed_paths` are admitted. Absolute paths, `..`, symlinks and multiply-linked regular files fail closed. Target code is parsed only where needed; it is never imported or executed. The core uses Python stdlib and contains no network client. Explicit network/shell/ambient-resource requests are rejected.

## Shadow-world proof

Current public task specifications are used read-only only:

- `tableau/server-client-python#1809`, retrieved 2026-08-18, open. Public code snapshot pinned to `development@aa9e3a0bd3114e0dbb7ec41abd4784483fb89277`. Classified `CAN_HANDLE` for a bounded shadow reproduction of the CSVImport off-by-one/case-normalization defects. The shadow emits a patch and regression acceptance evidence but performs no upstream write and makes no claim to have repaired all five upstream bugs.
- `grodriguez4321-tech/learning-snake#24`, retrieved 2026-08-18, open. Classified `NEEDS_OTHER_WORKER`: Qt learner UI, curriculum design, Windows visual/native QA and broad product integration exceed the immutable worker capability set.

## Hard locks

`PRODUCTION_MUTATION=0` · `SUPABASE_MUTATION=0` · `NORTHFLANK_MUTATION=0` · `RUNTIME017_MUTATIONS=0` · `THRESHOLD_WEIGHT_TUNING=0` · `REAL_TRADING=0` · `OUTGOING_SPEND_USD=0`.

`READY_FOR_ATM_INTEGRATION` is deliberately not claimed by ARQ; it is reserved to independent exact-head audit.
