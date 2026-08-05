# H-011 reproducible runtime contract

The H-011 runtime image is a paper-only artifact. It must never contain trading
credentials, wallet material or authenticated order capability.

## Immutable inputs

- Base image: `python:3.11-slim` index digest
  `sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7`.
- Expected Linux ARM64 child manifest:
  `sha256:20eadabc42589e6543b24a64ab305b9895e9fcf6dbb2cadb14812f394ecdbadf`.
- `requirements-h011-v3-runtime.txt` is the complete runtime-only lock.
- `requirements-h011-v3-test.txt` is the disjoint test-only lock.
- `requirements-h011-v3.txt` is the historical CI compatibility lock. It must
  equal the exact runtime/test union mechanically; it is not used by the image.
- Every package is exactly pinned and every permitted CPython 3.11 ARM64/AMD64
  wheel is SHA-256 allowlisted.
- Complete-lock installation uses `--require-hashes`, `--only-binary=:all:` and
  `--no-deps`. Runtime installation also uses `--no-compile`.

## Deterministic build

`tools/verify_h011_arm64_reproducibility.sh` derives `SOURCE_DATE_EPOCH` from
the exact Git commit, builds a canonical `git archive`, disables provenance and
SBOM variation, rewrites output timestamps, and performs two no-cache ARM64
builds in independent BuildKit builders. The manifest, config, layer digest set,
base digest, dependency hash set and rootfs diff-ID hash must be byte-identical.

Before QEMU or Buildx starts, `tools/verify_h011_shell_harness.py` and the
harness self-test prove Bash syntax, strict-variable safety, output-directory
initialization and fail-closed `result.env` creation for controlled failures.

The same gate verifies the repository-wide paper-only boundary, wheel
availability on ARM64 and AMD64, absence of pytest from the runtime image,
loopback-only host binding, all control-plane endpoints, graceful shutdown and
persistent restart/replay continuity.

## Workflow boundary

- `PERMANENT_PRODUCT_CONTROL=.github/workflows/h011-arm64-reproducibility.yml`:
  ongoing exact-head regression prevention. It is read-only, uses pinned actions
  and performs no deployment or infrastructure mutation.
- `PERMANENT_PRODUCT_CONTROL=.github/workflows/h011-integrity.yml`: repository
  paper-only and lock-consistency regression prevention.
- `TEMPORARY_VALIDATION_HARNESS=NONE_IN_FINAL_TREE`.
- Repository-owned verification scripts remain because they are deterministic,
  self-tested and runnable outside GitHub Actions.
## Docker context isolation

H-011 filtering is isolated in
`polymarket/Dockerfile.h011-v3.dockerignore`. The repository root has no
restrictive `.dockerignore`, so the independent root Dockerfile retains its
required `senecio_polymarket/**` context.
## Deterministic runtime baseline

The ARM64 runtime gate publishes a fixed synthetic committed scan into the
shared results volume before starting the service. The service then runs with
`H011_RUNTIME_DIAGNOSTIC_ONLY=true`, so endpoint, shutdown and restart/replay
checks do not depend on current Polymarket network state. Both pre- and
post-restart raw-chain sequences must be decimal integers, and the committed
baseline identity must match the fixed run and scan IDs.
