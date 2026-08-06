#!/usr/bin/env python3
"""One-shot transformation for PR30 independent OCI build evidence."""
from pathlib import Path

script = Path("tools/verify_h011_arm64_reproducibility.sh")
text = script.read_text()

old_locks = '''sha256sum \\
  "$ROOT/polymarket/requirements-h011-v3-runtime.txt" \\
  "$ROOT/polymarket/requirements-h011-v3-test.txt" \\
  "$ROOT/polymarket/requirements-h011-v3.txt" \\
  | sort > "$EVIDENCE/build-1-lock-hash-set.txt"
cp "$EVIDENCE/build-1-lock-hash-set.txt" "$EVIDENCE/build-2-lock-hash-set.txt"
'''
new_locks = '''sha256sum "$ROOT/polymarket/requirements-h011-v3-runtime.txt" \\
  | awk '{print $1"  /app/polymarket/requirements-h011-v3-runtime.txt"}' \\
  > "$EVIDENCE/source-runtime-lock-sha256.txt"
'''
if text.count(old_locks) != 1:
    raise SystemExit(f"pre-build lock evidence block count={text.count(old_locks)}")
text = text.replace(old_locks, new_locks)

old_base = '''printf '%s\\n' "$BASE_INDEX" > "$EVIDENCE/base-index-digest.txt"
printf '%s\\n' "$BASE_ARM64" > "$EVIDENCE/base-arm64-child-digest.txt"
printf '%s\\n' "$BASE_ARM64" > "$EVIDENCE/build-1-base-digest.txt"
printf '%s\\n' "$BASE_ARM64" > "$EVIDENCE/build-2-base-digest.txt"
[[ "$BASE_INDEX" == "$EXPECTED_BASE_INDEX" ]] \\
  || die "BASE_INDEX_DIGEST_CONTRADICTION"
[[ "$BASE_ARM64" == "$EXPECTED_BASE_ARM64" ]] \\
  || die "BASE_ARM64_CHILD_DIGEST_CONTRADICTION"
'''
new_base = '''printf '%s\\n' "$BASE_INDEX" > "$EVIDENCE/base-index-digest.txt"
printf '%s\\n' "$BASE_ARM64" > "$EVIDENCE/base-arm64-child-digest.txt"
[[ "$BASE_INDEX" == "$EXPECTED_BASE_INDEX" ]] \\
  || die "BASE_INDEX_DIGEST_CONTRADICTION"
[[ "$BASE_ARM64" == "$EXPECTED_BASE_ARM64" ]] \\
  || die "BASE_ARM64_CHILD_DIGEST_CONTRADICTION"
docker buildx imagetools inspect "python:3.11-slim@${BASE_ARM64}" --raw \\
  > "$EVIDENCE/base-arm64-manifest.json"
OBSERVED_BASE_ARM64="sha256:$(sha256sum "$EVIDENCE/base-arm64-manifest.json" | awk '{print $1}')"
[[ "$OBSERVED_BASE_ARM64" == "$EXPECTED_BASE_ARM64" ]] \\
  || die "BASE_ARM64_MANIFEST_DIGEST_CONTRADICTION"
jq -r '.layers[].digest' "$EVIDENCE/base-arm64-manifest.json" \\
  > "$EVIDENCE/base-arm64-layer-digests.txt"
[[ -s "$EVIDENCE/base-arm64-layer-digests.txt" ]] \\
  || die "BASE_ARM64_LAYER_SET_MISSING"
'''
if text.count(old_base) != 1:
    raise SystemExit(f"pre-build base evidence block count={text.count(old_base)}")
text = text.replace(old_base, new_base)

inspect_anchor = '''  printf '%s\\n' "$manifest" > "$EVIDENCE/$prefix-manifest-digest.txt"
  printf '%s\\n' "$config" > "$EVIDENCE/$prefix-config-digest.txt"
}
'''
inspect_new = '''  printf '%s\\n' "$manifest" > "$EVIDENCE/$prefix-manifest-digest.txt"
  printf '%s\\n' "$config" > "$EVIDENCE/$prefix-config-digest.txt"
  python3 "$ROOT/tools/verify_h011_oci_build_evidence.py" \\
    --oci-dir "$dir" \\
    --base-manifest "$EVIDENCE/base-arm64-manifest.json" \\
    --expected-base-digest "$EXPECTED_BASE_ARM64" \\
    --runtime-lock-source "$ROOT/polymarket/requirements-h011-v3-runtime.txt" \\
    --output-dir "$EVIDENCE" \\
    --prefix "$prefix" \\
    > "$EVIDENCE/$prefix-derived-build-evidence.stdout"
}
'''
if text.count(inspect_anchor) != 1:
    raise SystemExit(f"inspect_oci evidence anchor count={text.count(inspect_anchor)}")
text = text.replace(inspect_anchor, inspect_new)

old_comparisons = '''require_equal \\
  "$EVIDENCE/build-1-base-digest.txt" \\
  "$EVIDENCE/build-2-base-digest.txt" \\
  "ARM64_BASE_DIGESTS_DIFFER"
require_equal \\
  "$EVIDENCE/build-1-lock-hash-set.txt" \\
  "$EVIDENCE/build-2-lock-hash-set.txt" \\
  "ARM64_LOCK_HASH_SETS_DIFFER"
'''
new_comparisons = '''require_equal \\
  "$EVIDENCE/base-arm64-layer-digests.txt" \\
  "$EVIDENCE/build-1-base-layer-digests.txt" \\
  "BUILD_1_BASE_LAYER_PREFIX_DIFFERS"
require_equal \\
  "$EVIDENCE/base-arm64-layer-digests.txt" \\
  "$EVIDENCE/build-2-base-layer-digests.txt" \\
  "BUILD_2_BASE_LAYER_PREFIX_DIFFERS"
require_equal \\
  "$EVIDENCE/source-runtime-lock-sha256.txt" \\
  "$EVIDENCE/build-1-runtime-lock-sha256.txt" \\
  "BUILD_1_RUNTIME_LOCK_DIFFERS_FROM_SOURCE"
require_equal \\
  "$EVIDENCE/source-runtime-lock-sha256.txt" \\
  "$EVIDENCE/build-2-runtime-lock-sha256.txt" \\
  "BUILD_2_RUNTIME_LOCK_DIFFERS_FROM_SOURCE"
require_equal \\
  "$EVIDENCE/build-1-base-digest.txt" \\
  "$EVIDENCE/build-2-base-digest.txt" \\
  "ARM64_BASE_DIGESTS_DIFFER"
require_equal \\
  "$EVIDENCE/build-1-runtime-lock-sha256.txt" \\
  "$EVIDENCE/build-2-runtime-lock-sha256.txt" \\
  "ARM64_RUNTIME_LOCK_HASHES_DIFFER"
'''
if text.count(old_comparisons) != 1:
    raise SystemExit(
        f"tautological comparison block count={text.count(old_comparisons)}"
    )
text = text.replace(old_comparisons, new_comparisons)

report_anchor = '''    "build_1_rootfs_hash": text("build-1-rootfs-hash.txt"),
    "build_2_rootfs_hash": text("build-2-rootfs-hash.txt"),
'''
report_new = '''    "build_1_rootfs_hash": text("build-1-rootfs-hash.txt"),
    "build_2_rootfs_hash": text("build-2-rootfs-hash.txt"),
    "build_1_base_digest": text("build-1-base-digest.txt"),
    "build_2_base_digest": text("build-2-base-digest.txt"),
    "build_1_runtime_lock_sha256": text("build-1-runtime-lock-sha256.txt"),
    "build_2_runtime_lock_sha256": text("build-2-runtime-lock-sha256.txt"),
'''
if text.count(report_anchor) != 1:
    raise SystemExit(f"result report anchor count={text.count(report_anchor)}")
text = text.replace(report_anchor, report_new)

for forbidden in (
    "build-1-lock-hash-set.txt",
    "build-2-lock-hash-set.txt",
    'cp "$EVIDENCE/build-1-lock-hash-set.txt"',
):
    if forbidden in text:
        raise SystemExit(f"tautological evidence marker remains: {forbidden}")
script.write_text(text)

checker = Path("tools/verify_h011_shell_harness.py")
ctext = checker.read_text()
required_marker = '    "container-initial-failure.log",\n)'
required_replacement = '''    "container-initial-failure.log",
    "verify_h011_oci_build_evidence.py",
    "base-arm64-manifest.json",
    "base-arm64-layer-digests.txt",
    "build-1-base-layer-digests.txt",
    "build-2-base-layer-digests.txt",
    "source-runtime-lock-sha256.txt",
    "build-1-runtime-lock-sha256.txt",
    "build-2-runtime-lock-sha256.txt",
    "BUILD_1_BASE_LAYER_PREFIX_DIFFERS",
    "BUILD_2_BASE_LAYER_PREFIX_DIFFERS",
    "BUILD_1_RUNTIME_LOCK_DIFFERS_FROM_SOURCE",
    "BUILD_2_RUNTIME_LOCK_DIFFERS_FROM_SOURCE",
    "ARM64_RUNTIME_LOCK_HASHES_DIFFER",
)'''
if ctext.count(required_marker) != 1:
    raise SystemExit(
        f"harness independent evidence marker count={ctext.count(required_marker)}"
    )
ctext = ctext.replace(required_marker, required_replacement)

audit_anchor = "    errors.extend(_multi_local_assignment_errors(text))\n"
audit_new = '''    errors.extend(_multi_local_assignment_errors(text))
    for marker in (
        "build-1-lock-hash-set.txt",
        "build-2-lock-hash-set.txt",
        'cp "$EVIDENCE/build-1-lock-hash-set.txt"',
    ):
        if marker in text:
            errors.append(f"tautological build evidence is forbidden: {marker}")
    if re.search(
        r"printf[^\\n]*\\$BASE_ARM64[^\\n]*build-[12]-base-digest\\.txt",
        text,
    ):
        errors.append("base digest evidence must be derived from each OCI build")
'''
if ctext.count(audit_anchor) != 1:
    raise SystemExit(f"harness audit insertion count={ctext.count(audit_anchor)}")
checker.write_text(ctext.replace(audit_anchor, audit_new))

docs = Path("docs/H011_REPRODUCIBLE_RUNTIME.md")
dtext = docs.read_text()
section = '''

## Independent per-build evidence

Each OCI archive is inspected independently after its builder completes. The
verifier checks that the image layer list begins with the exact ARM64 child
manifest layer sequence of the pinned Python base, then reconstructs the final
runtime lock from that OCI root filesystem and compares its bytes and SHA-256
with the source runtime lock. Evidence for build 1 is never copied or reused as
evidence for build 2. Tautological pre-build base or lock comparisons are
rejected by the repository-owned shell audit.
'''
if "## Independent per-build evidence" not in dtext:
    docs.write_text(dtext.rstrip() + section)
