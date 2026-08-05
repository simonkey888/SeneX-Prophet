# H-011 reproducible runtime contract

The H-011 runtime image is a paper-only artifact. It must never contain trading
credentials, wallet material or authenticated order capability.

## Immutable inputs

- Base image: `python:3.11-slim` index digest
  `sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7`.
- Expected Linux ARM64 child manifest:
  `sha256:20eadabc42589e6543b24a64ab305b9895e9fcf6dbb2cadb14812f394ecdbadf`.
- Runtime and test dependencies are separate complete locks. Every package is
  exactly pinned and every permitted ARM64/AMD64 wheel is SHA-256 allowlisted.
- Runtime installation uses `--require-hashes`, `--only-binary=:all:`,
  `--no-deps` and `--no-compile`.

## Deterministic build

`tools/verify_h011_arm64_reproducibility.sh` derives `SOURCE_DATE_EPOCH` from
the exact Git commit, builds a canonical `git archive`, disables provenance and
SBOM variation, rewrites output timestamps, and performs two no-cache ARM64
builds in independent BuildKit builders. The manifest, config, layer digest set
and rootfs diff-ID hash must be byte-identical.

The same gate verifies the repository-wide paper-only boundary, wheel
availability on ARM64 and AMD64, loopback-only binding, all control-plane
endpoints, graceful shutdown and persistent restart/replay continuity.
