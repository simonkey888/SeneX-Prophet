# H-011 V3 — Raw Artifact Transaction Design

## Status: ARCHITECTURE FINALIZED — E1–E7

- Design base code: `18f8e6dea12c25a4dc338b0a0fdb2bccc417540b`
- Branch: `feat/h011-v3-control-plane-coverage`
- Production: `2f8503533543832147caf4c8e97a0cc6f5af3cbc` — untouched
- PR: `#5` — Draft
- Runtime implementation: paused

This file is the normative contract for immutable per-scan raw publication. There is one stager, one publisher, one recovery path, and one strict verifier. Any unresolved integrity condition is fail-closed.

---

## 1. Paths and legacy policy

```text
results/h011_v3/raw/                    legacy; never auto-migrated
results/h011_v3/raw_chain_v1/           new immutable chain
results/h011_v3/raw_chain_v1/.pending/  transaction staging
results/h011_v3/raw_chain_v1/.quarantine/
results/h011_v3/raw_chain_v1/.eligibility_state.json
results/h011_v3/raw_chain_v1/<prefix>.lock
```

Legacy files are excluded from the new glob and INV-005 chain. A non-empty new-chain directory without valid manifests is `BOOTSTRAP_REQUIRED`.

---

## 2. Canonical bytes and hashes

```python
def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
```

List order is preserved. NaN and Infinity are rejected. SHA-256 strings are lowercase hexadecimal.

```python
payload_sha256 = sha256(canonical_json_bytes(payload)).hexdigest()
canonical_events_sha256 = sha256(canonical_json_bytes(events)).hexdigest()
```

Manifest contract:

```python
without_hash = {k: v for k, v in entry.items() if k != "manifest_hash"}
manifest_hash = sha256(canonical_json_bytes(without_hash)).hexdigest()
entry["manifest_hash"] = manifest_hash
manifest_file_bytes = canonical_json_bytes(entry)
candidate_manifest_bytes_sha256 = sha256(manifest_file_bytes).hexdigest()
```

Manifest files contain exactly `manifest_file_bytes`, without a trailing newline. Recovery writes frozen marker bytes; it never reconstructs timestamps or entries.

Sidecar bytes are exactly:

```text
<64 lowercase hexadecimal characters>\n
```

Exactly 65 ASCII bytes.

---

## 3. Strict raw artifact contract

`load_raw_events_strict(path)` rejects:

- invalid or truncated gzip;
- invalid UTF-8;
- blank or malformed JSONL;
- non-object records;
- missing required fields;
- invalid payload hashes;
- NaN or Infinity.

It never skips lines.

```python
@dataclass(frozen=True)
class SealedRawArtifact:
    version: str                    # h011-sealed-raw-v1
    staging_filename: str
    final_name: str
    run_id: str
    scan_id: str
    event_count: int
    condition_ids: tuple[str, ...]
    file_sha256: str
    canonical_events_sha256: str
    size_bytes: int
    sealed_at: str
    device_id: int
    inode: int
```

Seal order:

```text
finish gzip stream
-> flush and close gzip
-> reopen staging O_RDONLY
-> fsync
-> fstat
-> close fd
-> chmod 0444
-> fsync .pending
-> strict reread
-> recompute metadata and hashes
-> return SealedRawArtifact
```

Under lock, publisher repeats `lstat`, rejects symlinks, verifies device/inode/size/hash/content, requires the target directory on the same filesystem, and verifies the final hardlink has the same device and inode.

---

## 4. Ownership and public API

```python
@dataclass(frozen=True)
class RawArtifactTransfer:
    sealed: SealedRawArtifact
    ownership_token: str
    staging_path: Path
```

`ownership_token` is a UUID4 identifier, not a credential.

`stager.transfer()` is the only `SEALED -> TRANSFERRED` transition. After transfer, the context manager never deletes staging and the durable marker is authoritative.

```python
def publish_raw_scan(
    directory: Path,
    transfer: RawArtifactTransfer,
    policy: ManifestPolicy,
) -> PublishResult:
    ...
```

Identity comes only from `transfer.sealed`. There is no external identity-field argument.

Lifecycle:

```text
OPEN -> SEALED -> TRANSFERRED -> PUBLISHED
OPEN/SEALED -> ABORTED_BEFORE_TRANSFER
OPEN/SEALED -> ABORTED_WITH_DIAGNOSTIC_EVIDENCE
TRANSFERRED -> RECOVERABLE_ERROR_AFTER_TRANSFER
TRANSFERRED -> BLOCKED_AFTER_TRANSFER
```

---

## 5. Explicit lock guard — E5

```python
@dataclass(frozen=True)
class RawChainLockGuard:
    directory: Path
    policy_prefix: str
    fd: int
    owner_pid: int
    token: str
```

Only `acquire_raw_chain_lock()` constructs guards. It acquires `fcntl.flock(fd, LOCK_EX)`, registers the token in a process-wide mutex-protected registry, yields the guard, unregisters the token, unlocks, and closes.

Every `*_under_lock` helper receives `guard` as its first positional argument.

```python
def assert_lock_guard(guard, directory, policy):
    assert guard.owner_pid == os.getpid()
    assert guard.directory == directory.resolve()
    assert guard.policy_prefix == policy.manifest_prefix
    assert fd_is_open(guard.fd)
    assert guard.token in ACTIVE_GUARDS
```

No thread-local mechanism and no nested acquisition are allowed.

```python
def recover_raw_transactions(directory, policy):
    with acquire_raw_chain_lock(directory, policy) as guard:
        return _recover_raw_transactions_under_lock(
            guard, directory, policy
        )
```

Runtime startup calls the public function. Publisher calls only the private under-lock function.

Unsupported flock refuses publication. INV-005 is FAIL if a chain or eligible scan exists, otherwise UNKNOWN.

---

## 6. Transaction marker v2 — E2, E3, E7

Complete schema:

```json
{
  "transaction_version": "h011-artifact-txn-v2",
  "transaction_uuid": "<uuid4>",
  "ownership_token": "<uuid4>",
  "status": "STAGED",
  "resolution": "ACTIVE",
  "sequence": 0,
  "run_id": "...",
  "scan_id": "...",
  "staging_filename": "...",
  "final_name": "...",
  "sidecar_name": "...",
  "manifest_name": "...",
  "device_id": 0,
  "inode": 0,
  "size_bytes": 0,
  "file_sha256": "<64 lowercase hex>",
  "canonical_events_sha256": "<64 lowercase hex>",
  "event_count": 0,
  "condition_ids": [],
  "previous_manifest_hash": null,
  "candidate_manifest": {},
  "candidate_manifest_bytes_base64": "...",
  "candidate_manifest_bytes_sha256": "<64 lowercase hex>",
  "manifest_created_at": "...",
  "failure_stage": null,
  "failure_type": null,
  "failure_message": null,
  "recoverable": true,
  "marker_integrity_sha256": "<64 lowercase hex>"
}
```

All fields are required. Failure fields may be null. `recoverable` is a required boolean.

Statuses:

```text
STAGED
ARTIFACT_PUBLISHED
SIDECAR_PUBLISHED
MANIFEST_PUBLISHED
COMMITTED
```

Resolutions:

```text
ACTIVE
BLOCKED
QUARANTINED
```

`marker_integrity_sha256` is SHA-256 of canonical JSON excluding itself.

Marker name:

```text
{manifest_prefix}_txn_{sequence:06d}_{transaction_uuid}.marker
```

### Initial creation: no replace

```python
def create_marker_no_replace(directory, marker_name, marker):
    temp = directory / f"{marker_name}.tmp.{uuid4()}"
    final = directory / marker_name
    write_exclusive_fsync(temp, canonical_marker_bytes(marker))
    os.link(temp, final)          # FileExistsError: BLOCK
    fsync_directory(directory)
    os.unlink(temp)
    fsync_directory(directory)
```

Initial placement must not use `rename` or `replace`.

### Existing update: intentional replace

```python
def update_existing_marker_atomic(directory, marker_path, marker):
    require_existing_regular_nonsymlink(marker_path)
    temp = directory / f"{marker_path.name}.tmp.{uuid4()}"
    write_exclusive_fsync(temp, canonical_marker_bytes(marker))
    os.replace(temp, marker_path)
    fsync_directory(directory)
```

### Exact marker validation

Before marker-directed filesystem access:

```python
decoded = base64.b64decode(
    marker["candidate_manifest_bytes_base64"],
    validate=True,
)
assert sha256(decoded).hexdigest() == marker[
    "candidate_manifest_bytes_sha256"
]
parsed = json.loads(decoded.decode("utf-8"))
assert parsed == marker["candidate_manifest"]
assert decoded == canonical_manifest_file_bytes(
    marker["candidate_manifest"]
)
assert compute_manifest_hash(parsed) == parsed["manifest_hash"]
```

Also verify marker integrity, schema, UUIDs, types, identity, sequence, hashes, and filename relationships.

Unsafe, absolute, separator-containing, `..`, NUL, non-canonical, or symlink paths are QUARANTINED. Manifest entries receive the same independent path-safety validation.

---

## 7. Verification APIs

```python
verify_candidate_logical(existing_entries, candidate_entry)
```

Pure verification of sequence, previous hash, manifest hash, unique filename/run/scan identity, types, and canonical bytes.

```python
verify_candidate_physical(
    directory,
    candidate_entry,
    policy,
    allowed_candidate_filename,
)
```

Runs after artifact and sidecar but before manifest. Allows only the current candidate to be temporarily unregistered. Verifies exact sidecar, file hash, strict gzip/JSONL, event count, condition IDs, canonical event hash, and payload hashes.

```python
verify_manifest_entry_physical(directory, entry, policy)
```

Strict physical verification for an already manifested entry. No candidate exception.

```python
verify_committing_transaction(directory, marker, entry, policy)
```

Runs at `MANIFEST_PUBLISHED`. Requires exact final trio, exact staging, only the current marker and staging, valid prior chain, and manifest bytes equal frozen candidate bytes.

```python
verify_committed_transaction(directory, marker, policy)
```

Runs while the COMMITTED marker remains after staging removal. Requires staging absent, exact final trio, exact candidate bytes, and a valid chain through the candidate.

```python
verify_raw_chain(directory, policy)
```

Steady state only. Requires valid sequence/hash linkage, unique identities, valid physical entries, and zero orphans, markers, pending files, or quarantine files.

---

## 8. Canonical transaction sequence — E1

One `RawChainLockGuard` covers the entire operation:

```text
1. acquire flock and guard
2. recover under lock
3. verify prior steady-state chain
4. validate eligibility
5. validate transfer and staging
6. evaluate identity/idempotency
7. reserve sequence under lock
8. build exact candidate and frozen bytes
9. verify candidate logical
10. create STAGED marker no-replace
11. fault AFTER_STAGED_FSYNC

12. hardlink artifact no-replace
13. fsync chain directory
14. update marker ARTIFACT_PUBLISHED
15. fault AFTER_ARTIFACT_FSYNC

16. publish sidecar no-replace
17. fsync chain directory
18. update marker SIDECAR_PUBLISHED
19. fault AFTER_SIDECAR_FSYNC

20. verify candidate physical

21. publish exact manifest bytes no-replace
22. fsync chain directory
23. update marker MANIFEST_PUBLISHED
24. fault AFTER_MANIFEST_FSYNC

25. verify committing transaction
26. update marker COMMITTED
27. fault AFTER_COMMITTED_FSYNC

28. remove staging first
29. fsync .pending
30. verify committed transaction while marker remains
31. remove marker last
32. fsync chain directory
33. verify steady-state chain
34. release flock
```

No validated snapshot or success is emitted before step 33.

If final steady-state verification fails, write a durable integrity incident in `.quarantine/`, set `BLOCKED_RAW_INTEGRITY`, make INV-005 FAIL, and preserve final components.

---

## 9. Recoverable cleanup — E6

The marker is always removed last.

```text
COMMITTED + staging present + exact final trio:
    remove staging
    fsync .pending
    keep marker
    verify committed transaction
    remove marker
    fsync chain directory
    CLEAN

COMMITTED + staging absent + exact final trio:
    verify committed transaction
    remove marker
    fsync chain directory
    CLEAN

COMMITTED + missing/invalid final component:
    BLOCK

COMMITTED + candidate bytes mismatch:
    BLOCK
```

A failed staging unlink leaves the COMMITTED marker. A failed marker unlink leaves the COMMITTED marker. Recovery retries CLEAN idempotently.

---

## 10. Diagnostic evidence — E4

`ABORTED_WITH_DIAGNOSTIC_EVIDENCE` applies before transfer when an event append, gzip write/flush, fsync, seal, strict reread, or unexpected internal operation fails after evidence exists.

Protocol:

```text
close handles when possible
-> chmod evidence read-only
-> compute best-effort SHA
-> hardlink staging no-replace into .quarantine
-> fsync .quarantine
-> unlink original staging
-> fsync .pending
-> write diagnostic JSON O_EXCL
-> fsync diagnostic and .quarantine
-> context manager does not delete evidence
```

If the hardlink fails, retain original staging and record its location; never delete it.

Diagnostic schema:

```json
{
  "diagnostic_version": "h011-raw-abort-v1",
  "diagnostic_uuid": "<uuid4>",
  "run_id": "...",
  "scan_id": "...",
  "failure_type": "...",
  "failure_message": "...",
  "failure_stage": "...",
  "event_count_written": 0,
  "evidence_filename": "...",
  "file_sha256": null,
  "created_at": "...",
  "recoverable": false,
  "diagnostic_integrity_sha256": "<64 lowercase hex>"
}
```

After transfer, the transaction marker remains authoritative. Recoverable failures keep resolution ACTIVE; unsafe automatic continuation sets BLOCKED; corrupt marker/path evidence sets QUARANTINED.

---

## 11. Recovery matrix

“Exact” includes names, safe paths, hashes, inode where applicable, identity, sequence, candidate bytes, and manifest linkage.

| Marker state | Observation | Action |
|---|---|---|
| STAGED | no final component | CONTINUE artifact |
| STAGED | exact artifact | CONTINUE as ARTIFACT_PUBLISHED |
| STAGED | exact artifact + sidecar | CONTINUE as SIDECAR_PUBLISHED |
| STAGED | exact trio | CONTINUE as MANIFEST_PUBLISHED |
| ARTIFACT_PUBLISHED | exact artifact | CONTINUE sidecar |
| ARTIFACT_PUBLISHED | exact artifact + sidecar | CONTINUE as SIDECAR_PUBLISHED |
| ARTIFACT_PUBLISHED | exact trio | CONTINUE as MANIFEST_PUBLISHED |
| SIDECAR_PUBLISHED | exact artifact + sidecar | CONTINUE manifest |
| SIDECAR_PUBLISHED | exact trio | CONTINUE as MANIFEST_PUBLISHED |
| MANIFEST_PUBLISHED | exact trio + exact staging + valid chain | COMMIT |
| COMMITTED | exact trio + staging present | CLEAN staging first |
| COMMITTED | exact trio + staging absent | CLEAN marker |
| any active state | present component not exact | BLOCK |
| any | prior chain invalid | BLOCK |
| any | duplicate sequence or transaction UUID | BLOCK |
| any | corrupt marker/schema/integrity | QUARANTINE |
| any | unsafe path or symlink | QUARANTINE |
| any | invalid base64/canonical candidate | QUARANTINE |
| no marker | orphan artifact/sidecar/manifest | BLOCK |
| any | stale temp | validate then CLEAN or QUARANTINE |
| resolution BLOCKED | any | preserve and refuse publication |
| resolution QUARANTINED | any | preserve and refuse publication |

Recovery is under the same lock, without sleeps.

---

## 12. Identity and retries

```text
same run_id + same scan_id + same file_sha256:
    IDEMPOTENT_SUCCESS; return existing entry

same run_id + same scan_id + different hash:
    BLOCK

same run_id + different scan_id:
    BLOCK

same scan_id + different run_id:
    BLOCK
```

No duplicate case creates a new sequence.

---

## 13. Eligibility and INV-005

```json
{
  "schema_version": "h011-eligibility-v1",
  "first_eligible_scan_seen": true,
  "first_eligible_scan_id": "...",
  "first_persistible_data_api_request_at": "...",
  "state_sha256": "<64 lowercase hex>"
}
```

State is monotonic: false may become true; true may not become false.

Write by unique temp, canonical bytes, file fsync, `os.replace`, and directory fsync.

Read rules:

```text
absent: first_eligible_scan_seen=false
valid: use persisted state
present but corrupt: BLOCKED_RAW_INTEGRITY; INV-005 FAIL
```

INV-005 is read-only:

```text
EMPTY_CHAIN + not eligible: UNKNOWN
EMPTY_CHAIN + eligible: FAIL
VALID_CHAIN + no unresolved evidence: PASS
VALID_CHAIN + marker/quarantine/orphan: FAIL
BOOTSTRAP_REQUIRED: FAIL
INVALID_CHAIN: FAIL
flock unsupported + chain/eligible: FAIL
flock unsupported + empty/not eligible: UNKNOWN
zero markets + empty/not eligible: NOT_APPLICABLE for current scan
```

---

## 14. Runtime fail-closed policy

```text
RecoverableMarketDataError:
    record DEGRADED; continue; no raw event for failed request

MarketRejected after Data API:
    append rejection event; continue

RawEventPersistenceError:
    stop; diagnostic evidence; no publish; no snapshot

RawArtifactTransactionError:
    stop; preserve marker/staging; no snapshot

IdentityCollisionError:
    stop; no sequence; no snapshot

UnexpectedInternalError before any event:
    ABORTED_BEFORE_TRANSFER

UnexpectedInternalError after any event:
    diagnostic evidence; no partial publish
```

Only a completed market loop may seal, transfer, and publish. Raw publication precedes transform and snapshot.

---

## 15. Fault injection and mechanical tests

Fault points:

```text
AFTER_STAGED_FSYNC
AFTER_ARTIFACT_FSYNC
AFTER_SIDECAR_FSYNC
AFTER_MANIFEST_FSYNC
AFTER_COMMITTED_FSYNC
```

Tests use subprocess A with `os._exit(99)` and a separate subprocess B for recovery. Same-process exceptions and sleep synchronization are not accepted.

Minimum coverage:

- canonical payload/event/manifest hashes;
- exact 65-byte sidecar;
- strict gzip/JSONL failures;
- device/inode/size checks;
- initial marker no-replace;
- marker update replace;
- marker integrity and required fields;
- exact candidate validation;
- path traversal and symlink rejection;
- all five crash points;
- COMMITTED cleanup with and without staging;
- orphan and duplicate detection;
- multi-process sequence contention;
- eligibility absent/valid/corrupt;
- INV-005 read-only;
- no snapshot on raw-integrity failure.

---

## 16. Implementation ownership and acceptance

Ownership:

- `RawScanStager`: append, seal, transfer, pre-transfer diagnostic evidence;
- one transaction module: marker, publish, recovery, cleanup;
- one strict verifier contract: candidate, manifested entry, committed transaction, steady state;
- INV-005: read-only verifier consumer;
- runtime: ordering only.

Legacy permissive loaders and alternate publisher paths are removed from the V3 call graph.

Acceptance requires:

1. exact sequence in section 8;
2. complete marker schema;
3. no-replace initial marker;
4. staging-first, marker-last cleanup;
5. explicit validated lock guard;
6. strict payload, sidecar, manifest, marker, path, pending, and quarantine verification;
7. recovery across all five crash windows;
8. fail-closed eligibility and runtime;
9. deterministic retries;
10. multi-process tests passing.

---

## 17. Closure

| ID | Contract | Status |
|---|---|---|
| D1–D8 | prior architectural corrections | RESOLVED |
| E1 | complete transaction sequence | RESOLVED |
| E2 | marker create vs update primitives | RESOLVED |
| E3 | complete integrity-bearing schema | RESOLVED |
| E4 | diagnostic evidence lifecycle | RESOLVED |
| E5 | explicit lock guard | RESOLVED |
| E6 | recoverable cleanup order | RESOLVED |
| E7 | exact candidate and path validation | RESOLVED |

**Zero architectural decisions remain open.**

`DESIGN DOC COMPLETO — LISTO PARA IMPLEMENTACIÓN MECÁNICA`

---

## 18. Phase II-C runtime integration contract

Phase II-C binds the already-audited transaction core to the real V3 runtime without changing its marker, publisher, or recovery contract.

### 18.1 Product and technical names

```text
Product: SENEX
Technical system: SENECIO H-011 V3
```

Historical module names remain unchanged to preserve imports and audit evidence.

### 18.2 Storage roots

Authoritative transactional root:

```text
/app/polymarket/results/h011_v3/raw_chain_v1
```

Legacy read-only boundary:

```text
/app/polymarket/results/v3/raw
/app/polymarket/results/v3/scans
/app/polymarket/results/v3/state
bundle_*.json
YYYY-MM-DD.events.jsonl.gz
v3_scan_*.jsonl
```

Legacy files are never imported, migrated, sequenced, or treated as committed evidence. New V3 scans do not create a legacy raw bundle or daily append artifact.

### 18.3 Raw envelopes

The canonical JSONL.gz artifact preserves observation order and contains `h011-raw-envelope-v1` objects for:

- Gamma discovery evidence from `/events/keyset`;
- canonical Gamma market payloads;
- Data API trade responses;
- CLOB leg 0 and leg 1 book responses;
- fee metadata used by the execution simulation;
- source errors;
- explicit post-fetch rejection evidence.

Calculated VWAP, net edge, control-plane invariants, funnel counts, snapshots, and final aggregate metrics are derived data and are not substituted for source responses.

### 18.4 Runtime order

```text
validate paper-only configuration
-> validate/open storage root
-> acquire raw-chain lock
-> run startup recovery
-> verify committed steady state
-> enable scanner and publication
-> create one hardened RawScanStager
-> append source evidence in observation order
-> complete the market loop
-> seal and transfer staging ownership
-> acquire raw-chain lock
-> recover pending transaction under the same lock
-> publish artifact/sidecar/manifest
-> verify steady state
-> release lock
-> build and atomically publish derived snapshot cache
```

No snapshot is published before the raw transaction reaches verified committed steady state.

### 18.5 Runtime states

```text
STARTING
RECOVERING
RUNNING
DEGRADED
BLOCKED_RAW_INTEGRITY
BLOCKED_STORAGE_UNVERIFIED
SCANNER_FAILED
STOPPING
```

Scanner and publication are disabled during recovery and in every blocked state. There is no legacy-writer fallback.

### 18.6 Committed reader

`h011_v3_committed_snapshot.py` validates:

- contiguous sequence;
- previous-manifest hash linkage;
- canonical manifest bytes and manifest hash;
- artifact SHA-256 and strict gzip/JSONL content;
- exact sidecar bytes;
- final mode `0444`;
- condition IDs, event count, and canonical event hash;
- no marker, marker-temp, pending, quarantine, or unowned chain residue.

The last valid manifest is selected by sequence, never by mtime or lexicographic artifact name.

`latest.json` remains a regenerable cache. It is served only when its `aggregate_metrics.raw_chain` binding matches the latest committed manifest.

### 18.7 API and health

The dashboard, `/api/v3/state`, `/api/v3/integrity`, and `/api/v3/replay` use the committed reader. `/livez` reports process liveness, `/readyz` reports runtime readiness, and `/healthz` combines runtime, chain, snapshot age, alerts, and invariant state.

### 18.8 Filesystem probe

`h011_v3_fs_probe.py` is manual and isolated. It creates only `senex_fs_probe_<uuid>` below an explicitly provided parent and tests:

```text
flock
hardlink
renameat2(RENAME_EXCHANGE)
file fsync
directory fsync
O_NOFOLLOW
chmod 0444
inode stability
same-filesystem behavior
separate-process persistence
owned cleanup
```

It is never run automatically against production. Northflank volume validation remains a mandatory pre-deploy gate.

### 18.9 Process supervision

The Docker image runs `h011_v3_runtime.py` as PID 1. It performs startup recovery, starts the HTTP process, supervises one scanner process at a time, forwards SIGTERM/SIGINT, disables publication on scanner or chain failure, and exposes diagnostic state without enabling orders.

### 18.10 Safety invariants

```text
paper_only=true
orders_enabled=false
live_capital_locked=true
legacy_mode=false
```

Phase II-C introduces no wallet, private key, order submission, fill, realized PnL, NAV, or capital path.
