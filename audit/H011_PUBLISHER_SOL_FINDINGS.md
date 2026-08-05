# H-011 Publisher SOL Audit Findings

Confirmed findings:

1. The marker temp source was hardlinked without explicit `follow_symlinks=False`.
2. The canonical marker was not verified by exact bytes and inode identity before caller ownership was consumed.
3. A canonical marker-temp residue without a final marker was ignored by manifest-chain inspection, permitting a subsequent publication instead of requiring recovery.
4. Existing fault tests injected Python exceptions and then ran cleanup; they did not exercise abrupt real process death.

Materialized hardening:

- exact temp and canonical marker byte/hash verification;
- inode/dev/size identity verification across hardlink publication;
- `follow_symlinks=False` on the marker hardlink;
- durable canonical-marker commit point before temporary-link removal;
- explicit recovery requirement for canonical marker-temp residues;
- real `multiprocessing` + `os._exit()` crash coverage;
- short-write ownership regression coverage;
- controlled `0444` manifest corruption fixture with permission restoration.

Authoritative materialized product commit:

`1eade0149da220843a7bc3fc756c22a1c543bae2`

Final audit/CI head:

`7f338fe704615f9520203b430f4f20b6964d2c3c`

Native read-only CI:

- workflow run `29603792793`: `completed/success`;
- publisher: 41 passed;
- transaction core: 173 passed;
- hostile crash matrix: 24 passed;
- complete H-011 V3: 485 passed;
- global: 505 passed plus the single inherited out-of-scope control-plane failure;
- focused FD/residue gate: 3 passed;
- compileall and diff-check: passed;
- no repository marker/temp residues.

Evidence artifact:

- artifact ID `8416046743`;
- digest `sha256:34163c3652ef70363a4cfbbd5db4b6ceb0f1fc78168ca21cbafa2ce9e8b96406`.

The one-shot patcher has been removed. The audit workflow has `contents: read`, performs no commit or push, and validates the already-materialized head directly.

Permanent limits:

- `paper_only=true`
- `orders_enabled=false`
- `live_capital_locked=true`
- Draft only; no merge or deployment authorized.
