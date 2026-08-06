#!/usr/bin/env python3
"""One-shot exact-tree transform for AUD NEXT_ROUND_ORDER=010."""
from pathlib import Path

QEMU_DIGEST = "sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
BUILDKIT_DIGEST = "sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new)


script_path = Path("tools/verify_h011_arm64_reproducibility.sh")
script = script_path.read_text(encoding="utf-8")

script = replace_once(
    script,
    '''  --controlled-failure)\n    MODE="controlled-failure"\n    EVIDENCE_ARG="${2:-}"\n    ;;\n  "")\n''',
    '''  --controlled-failure)\n    MODE="controlled-failure"\n    EVIDENCE_ARG="${2:-}"\n    ;;\n  --identity-probe)\n    MODE="identity-probe"\n    EVIDENCE_ARG="${2:-}"\n    ;;\n  "")\n''',
    "identity mode",
)

old_vars = '''EXPECTED_BASE_INDEX="sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7"\nEXPECTED_BASE_ARM64="sha256:20eadabc42589e6543b24a64ab305b9895e9fcf6dbb2cadb14812f394ecdbadf"\nBASE_REFERENCE="python:3.11-slim@${EXPECTED_BASE_INDEX}"\nRUN_ID="${GITHUB_RUN_ID:-local}"\nHEAD_SHA="unknown"\nHEAD_TREE="unknown"\nSOURCE_DATE_EPOCH="0"\nRESULT_WRITTEN="0"\nCONTAINER="senex-h011-repro-${RUN_ID}"\nIMAGE="senex-h011-repro:${GITHUB_SHA:-local}"\nWORK="${RUNNER_TEMP:-/tmp}/senex-h011-arm64-repro-${RUN_ID}-$$"\n'''
new_vars = f'''EXPECTED_BASE_INDEX="sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7"\nEXPECTED_BASE_ARM64="sha256:20eadabc42589e6543b24a64ab305b9895e9fcf6dbb2cadb14812f394ecdbadf"\nBASE_REFERENCE="python:3.11-slim@${{EXPECTED_BASE_INDEX}}"\nQEMU_BINFMT_DIGEST="{QEMU_DIGEST}"\nQEMU_BINFMT_REFERENCE="docker.io/tonistiigi/binfmt@${{QEMU_BINFMT_DIGEST}}"\nBUILDKIT_DIGEST="{BUILDKIT_DIGEST}"\nBUILDKIT_REFERENCE="docker.io/moby/buildkit@${{BUILDKIT_DIGEST}}"\nRUN_ID="${{GITHUB_RUN_ID:-local}}"\nEVENT_SHA="${{GITHUB_SHA:-unknown}}"\nHEAD_SHA="unknown"\nHEAD_TREE="unknown"\nCANDIDATE_HEAD_SHA="unknown"\nCANDIDATE_HEAD_TREE="unknown"\nSOURCE_DATE_EPOCH="0"\nRESULT_WRITTEN="0"\nCONTAINER="senex-h011-repro-${{RUN_ID}}"\nWORK="${{RUNNER_TEMP:-/tmp}}/senex-h011-arm64-repro-${{RUN_ID}}-$$"\n'''
script = replace_once(script, old_vars, new_vars, "immutable variables")

old_env = '''    printf 'HEAD_SHA=%s\\n' "$HEAD_SHA"\n    printf 'HEAD_TREE=%s\\n' "$HEAD_TREE"\n  } > "$EVIDENCE/result.env"\n  python3 - "$EVIDENCE" "$status" "$sanitized" "$HEAD_SHA" "$HEAD_TREE" <<'PY' || true\n'''
new_env = '''    printf 'HEAD_SHA=%s\\n' "$HEAD_SHA"\n    printf 'HEAD_TREE=%s\\n' "$HEAD_TREE"\n    printf 'CANDIDATE_HEAD_SHA=%s\\n' "$CANDIDATE_HEAD_SHA"\n    printf 'CANDIDATE_HEAD_TREE=%s\\n' "$CANDIDATE_HEAD_TREE"\n    printf 'EVENT_SHA=%s\\n' "$EVENT_SHA"\n    printf 'IMAGE_TAG=%s\\n' "${IMAGE:-unassigned}"\n    printf 'QEMU_BINFMT_REFERENCE=%s\\n' "$QEMU_BINFMT_REFERENCE"\n    printf 'QEMU_BINFMT_DIGEST=%s\\n' "$QEMU_BINFMT_DIGEST"\n    printf 'BUILDKIT_REFERENCE=%s\\n' "$BUILDKIT_REFERENCE"\n    printf 'BUILDKIT_DIGEST=%s\\n' "$BUILDKIT_DIGEST"\n  } > "$EVIDENCE/result.env"\n  python3 - "$EVIDENCE" "$status" "$sanitized" "$HEAD_SHA" "$HEAD_TREE" "$CANDIDATE_HEAD_SHA" "$CANDIDATE_HEAD_TREE" "$EVENT_SHA" "${IMAGE:-unassigned}" "$QEMU_BINFMT_REFERENCE" "$QEMU_BINFMT_DIGEST" "$BUILDKIT_REFERENCE" "$BUILDKIT_DIGEST" <<'PY' || true\n'''
script = replace_once(script, old_env, new_env, "result env fields")

script = replace_once(
    script,
    '''status, reason, head_sha, head_tree = sys.argv[2:]\n''',
    '''(status, reason, head_sha, head_tree, candidate_head_sha, candidate_head_tree,\n event_sha, image_tag, qemu_reference, qemu_digest, buildkit_reference,\n buildkit_digest) = sys.argv[2:]\n''',
    "result argv",
)
script = replace_once(
    script,
    '''    "head_tree": head_tree,\n    "source_date_epoch": text("source-date-epoch.txt"),\n''',
    '''    "head_tree": head_tree,\n    "candidate_head_sha": candidate_head_sha,\n    "candidate_head_tree": candidate_head_tree,\n    "event_sha": event_sha,\n    "image_tag": image_tag,\n    "image_id": text("runtime-image-id.txt"),\n    "image_revision_label": text("runtime-image-revision-label.txt"),\n    "qemu_binfmt_reference": qemu_reference,\n    "qemu_binfmt_digest": qemu_digest,\n    "buildkit_reference": buildkit_reference,\n    "buildkit_digest": buildkit_digest,\n    "buildkit_version": text("buildkit-version.txt"),\n    "source_date_epoch": text("source-date-epoch.txt"),\n''',
    "result json identity",
)

old_git = '''if git -C "$ROOT" rev-parse HEAD >/dev/null 2>&1; then\n  HEAD_SHA="$(git -C "$ROOT" rev-parse HEAD)"\n  HEAD_TREE="$(git -C "$ROOT" rev-parse HEAD^{{tree}})"\n  SOURCE_DATE_EPOCH="$(git -C "$ROOT" show -s --format=%ct HEAD)"\nfi\nprintf '%s\\n' "$HEAD_SHA" > "$EVIDENCE/head-sha.txt"\nprintf '%s\\n' "$HEAD_TREE" > "$EVIDENCE/head-tree.txt"\nprintf '%s\\n' "$SOURCE_DATE_EPOCH" > "$EVIDENCE/source-date-epoch.txt"\n\nif [[ "$MODE" == "controlled-failure" ]]; then\n'''
new_git = '''if git -C "$ROOT" rev-parse HEAD >/dev/null 2>&1; then\n  HEAD_SHA="$(git -C "$ROOT" rev-parse HEAD)"\n  HEAD_TREE="$(git -C "$ROOT" rev-parse HEAD^{tree})"\n  SOURCE_DATE_EPOCH="$(git -C "$ROOT" show -s --format=%ct HEAD)"\nfi\nCANDIDATE_HEAD_SHA="$HEAD_SHA"\nCANDIDATE_HEAD_TREE="$HEAD_TREE"\nIMAGE="senex-h011-repro:${CANDIDATE_HEAD_SHA}"\nIMAGE_REVISION_LABEL_EXPECTED="$CANDIDATE_HEAD_SHA"\nprintf '%s\\n' "$HEAD_SHA" > "$EVIDENCE/head-sha.txt"\nprintf '%s\\n' "$HEAD_TREE" > "$EVIDENCE/head-tree.txt"\nprintf '%s\\n' "$CANDIDATE_HEAD_SHA" > "$EVIDENCE/candidate-head-sha.txt"\nprintf '%s\\n' "$CANDIDATE_HEAD_TREE" > "$EVIDENCE/candidate-head-tree.txt"\nprintf '%s\\n' "$EVENT_SHA" > "$EVIDENCE/event-sha.txt"\nprintf '%s\\n' "$IMAGE" > "$EVIDENCE/image-tag.txt"\nprintf '%s\\n' "$SOURCE_DATE_EPOCH" > "$EVIDENCE/source-date-epoch.txt"\n{\n  printf 'CANDIDATE_HEAD_SHA=%s\\n' "$CANDIDATE_HEAD_SHA"\n  printf 'CANDIDATE_HEAD_TREE=%s\\n' "$CANDIDATE_HEAD_TREE"\n  printf 'EVENT_SHA=%s\\n' "$EVENT_SHA"\n  printf 'IMAGE_TAG=%s\\n' "$IMAGE"\n  printf 'IMAGE_REVISION_LABEL_EXPECTED=%s\\n' "$IMAGE_REVISION_LABEL_EXPECTED"\n  printf 'QEMU_BINFMT_REFERENCE=%s\\n' "$QEMU_BINFMT_REFERENCE"\n  printf 'QEMU_BINFMT_DIGEST=%s\\n' "$QEMU_BINFMT_DIGEST"\n  printf 'BUILDKIT_REFERENCE=%s\\n' "$BUILDKIT_REFERENCE"\n  printf 'BUILDKIT_DIGEST=%s\\n' "$BUILDKIT_DIGEST"\n} > "$EVIDENCE/candidate-identity.env"\n\nif [[ "$MODE" == "identity-probe" ]]; then\n  exit 0\nfi\n\nif [[ "$MODE" == "controlled-failure" ]]; then\n'''
script = replace_once(script, old_git, new_git, "candidate identity")

old_self = '''  grep -qx 'FAILURE_REASON=CONTROLLED_SELF_TEST_FAILURE' "$nested/result.env" \\\n    || die "CONTROLLED_FAILURE_REASON_INVALID"\n  python3 - "$EVIDENCE/harness-self-test.json" <<'PY'\n'''
new_self = '''  grep -qx 'FAILURE_REASON=CONTROLLED_SELF_TEST_FAILURE' "$nested/result.env" \\\n    || die "CONTROLLED_FAILURE_REASON_INVALID"\n  (cd "$nested" && sha256sum -c SHA256SUMS) \\\n    > "$EVIDENCE/controlled-failure-checksums.log" \\\n    || die "CONTROLLED_FAILURE_CHECKSUMS_INVALID"\n  local identity="$EVIDENCE/nested/identity-probe"\n  local simulated_event="1111111111111111111111111111111111111111"\n  env -i \\\n    PATH="$PATH" \\\n    HOME="${HOME:-/tmp}" \\\n    GITHUB_SHA="$simulated_event" \\\n    bash "$ROOT/tools/verify_h011_arm64_reproducibility.sh" \\\n      --identity-probe "$identity"\n  grep -qx "CANDIDATE_HEAD_SHA=$HEAD_SHA" "$identity/candidate-identity.env" \\\n    || die "IDENTITY_SELF_TEST_CANDIDATE_SHA_INVALID"\n  grep -qx "CANDIDATE_HEAD_TREE=$HEAD_TREE" "$identity/candidate-identity.env" \\\n    || die "IDENTITY_SELF_TEST_CANDIDATE_TREE_INVALID"\n  grep -qx "EVENT_SHA=$simulated_event" "$identity/candidate-identity.env" \\\n    || die "IDENTITY_SELF_TEST_EVENT_SHA_INVALID"\n  grep -qx "IMAGE_TAG=senex-h011-repro:$HEAD_SHA" "$identity/candidate-identity.env" \\\n    || die "IDENTITY_SELF_TEST_IMAGE_TAG_INVALID"\n  grep -qx "IMAGE_REVISION_LABEL_EXPECTED=$HEAD_SHA" "$identity/candidate-identity.env" \\\n    || die "IDENTITY_SELF_TEST_REVISION_LABEL_INVALID"\n  (cd "$identity" && sha256sum -c SHA256SUMS) \\\n    > "$EVIDENCE/identity-probe-checksums.log" \\\n    || die "IDENTITY_SELF_TEST_CHECKSUMS_INVALID"\n  python3 - "$EVIDENCE/harness-self-test.json" <<'PY'\n'''
script = replace_once(script, old_self, new_self, "self-test checks")

script = replace_once(
    script,
    '''    "result_env_written_on_every_controlled_failure": "PASS",\n    "harness_self_test": "PASS",\n''',
    '''    "result_env_written_on_every_controlled_failure": "PASS",\n    "controlled_failure_artifact_checksums": "PASS",\n    "pr_event_candidate_identity": "PASS",\n    "harness_self_test": "PASS",\n''',
    "self-test report",
)

script = replace_once(
    script,
    '''    --driver docker-container \\\n    --use >/dev/null\n''',
    '''    --driver docker-container \\\n    --driver-opt "image=$BUILDKIT_REFERENCE" \\\n    --use >/dev/null\n''',
    "internal buildkit pin",
)

script = script.replace('--build-arg SENEX_CODE_SHA="$HEAD_SHA"', '--build-arg SENEX_CODE_SHA="$CANDIDATE_HEAD_SHA"')
script = script.replace('-e SENECIO_CODE_SHA="$HEAD_SHA"', '-e SENECIO_CODE_SHA="$CANDIDATE_HEAD_SHA"')
script = script.replace('--arg sha "$HEAD_SHA"', '--arg sha "$CANDIDATE_HEAD_SHA"')

runtime_anchor = '''docker image inspect "$IMAGE" | jq -S . \\\n  > "$EVIDENCE/runtime-image-inspect.json"\n[[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}/{{.Os}}')" == "arm64/linux" ]] \\\n  || die "RUNTIME_PLATFORM_MISMATCH"\n'''
runtime_new = '''docker image inspect "$IMAGE" | jq -S . \\\n  > "$EVIDENCE/runtime-image-inspect.json"\nIMAGE_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"\nIMAGE_REVISION_LABEL="$(docker image inspect "$IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"\nprintf '%s\\n' "$IMAGE_ID" > "$EVIDENCE/runtime-image-id.txt"\nprintf '%s\\n' "$IMAGE_REVISION_LABEL" > "$EVIDENCE/runtime-image-revision-label.txt"\n[[ "$IMAGE" == "senex-h011-repro:${CANDIDATE_HEAD_SHA}" ]] \\\n  || die "RUNTIME_IMAGE_TAG_NOT_CANDIDATE_HEAD"\n[[ "$IMAGE_REVISION_LABEL" == "$CANDIDATE_HEAD_SHA" ]] \\\n  || die "RUNTIME_IMAGE_REVISION_LABEL_NOT_CANDIDATE_HEAD"\n[[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}/{{.Os}}')" == "arm64/linux" ]] \\\n  || die "RUNTIME_PLATFORM_MISMATCH"\n'''
script = replace_once(script, runtime_anchor, runtime_new, "runtime identity assertion")

comparison_anchor = '''require_equal \\\n  "$EVIDENCE/build-1-runtime-lock-sha256.txt" \\\n  "$EVIDENCE/build-2-runtime-lock-sha256.txt" \\\n  "ARM64_RUNTIME_LOCK_HASHES_DIFFER"\n\ndocker buildx build \\\n'''
comparison_new = '''require_equal \\\n  "$EVIDENCE/build-1-runtime-lock-sha256.txt" \\\n  "$EVIDENCE/build-2-runtime-lock-sha256.txt" \\\n  "ARM64_RUNTIME_LOCK_HASHES_DIFFER"\nBUILDKIT_VERSION_1="$(awk '$1=="BuildKit:" {print $2; exit}' "$EVIDENCE/build-1-builder.txt")"\nBUILDKIT_VERSION_2="$(awk '$1=="BuildKit:" {print $2; exit}' "$EVIDENCE/build-2-builder.txt")"\n[[ -n "$BUILDKIT_VERSION_1" && "$BUILDKIT_VERSION_1" == "$BUILDKIT_VERSION_2" ]] \\\n  || die "BUILDKIT_VERSION_MISSING_OR_DIFFERENT"\nprintf '%s\\n' "$BUILDKIT_VERSION_1" > "$EVIDENCE/buildkit-version.txt"\n{\n  printf 'QEMU_BINFMT_REFERENCE=%s\\n' "$QEMU_BINFMT_REFERENCE"\n  printf 'QEMU_BINFMT_DIGEST=%s\\n' "$QEMU_BINFMT_DIGEST"\n  printf 'BUILDKIT_REFERENCE=%s\\n' "$BUILDKIT_REFERENCE"\n  printf 'BUILDKIT_DIGEST=%s\\n' "$BUILDKIT_DIGEST"\n  printf 'BUILDKIT_VERSION=%s\\n' "$BUILDKIT_VERSION_1"\n} > "$EVIDENCE/container-toolchain.env"\n\ndocker buildx build \\\n'''
script = replace_once(script, comparison_anchor, comparison_new, "toolchain evidence")

identity_append_anchor = '''[[ "$IMAGE_REVISION_LABEL" == "$CANDIDATE_HEAD_SHA" ]] \\\n  || die "RUNTIME_IMAGE_REVISION_LABEL_NOT_CANDIDATE_HEAD"\n[[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}/{{.Os}}')" == "arm64/linux" ]] \\\n'''
identity_append_new = '''[[ "$IMAGE_REVISION_LABEL" == "$CANDIDATE_HEAD_SHA" ]] \\\n  || die "RUNTIME_IMAGE_REVISION_LABEL_NOT_CANDIDATE_HEAD"\n{\n  printf 'CANDIDATE_HEAD_SHA=%s\\n' "$CANDIDATE_HEAD_SHA"\n  printf 'CANDIDATE_HEAD_TREE=%s\\n' "$CANDIDATE_HEAD_TREE"\n  printf 'EVENT_SHA=%s\\n' "$EVENT_SHA"\n  printf 'IMAGE_TAG=%s\\n' "$IMAGE"\n  printf 'IMAGE_ID=%s\\n' "$IMAGE_ID"\n  printf 'IMAGE_REVISION_LABEL=%s\\n' "$IMAGE_REVISION_LABEL"\n  printf 'QEMU_BINFMT_REFERENCE=%s\\n' "$QEMU_BINFMT_REFERENCE"\n  printf 'QEMU_BINFMT_DIGEST=%s\\n' "$QEMU_BINFMT_DIGEST"\n  printf 'BUILDKIT_REFERENCE=%s\\n' "$BUILDKIT_REFERENCE"\n  printf 'BUILDKIT_DIGEST=%s\\n' "$BUILDKIT_DIGEST"\n  printf 'BUILDKIT_VERSION=%s\\n' "$BUILDKIT_VERSION_1"\n} > "$EVIDENCE/runtime-image-identity.env"\n[[ "$(wc -l < "$EVIDENCE/runtime-image-identity.env")" -eq 11 ]] \\\n  || die "RUNTIME_IMAGE_IDENTITY_EVIDENCE_INCOMPLETE"\n[[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}/{{.Os}}')" == "arm64/linux" ]] \\\n'''
script = replace_once(script, identity_append_anchor, identity_append_new, "runtime identity evidence")

for forbidden in (
    'IMAGE="senex-h011-repro:${GITHUB_SHA:-local}"',
    'moby/buildkit:buildx-stable-1',
):
    if forbidden in script:
        raise SystemExit(f"forbidden mutable or ambiguous marker remains: {forbidden}")

script_path.write_text(script, encoding="utf-8")

harness_path = Path("tools/verify_h011_shell_harness.py")
harness = harness_path.read_text(encoding="utf-8")
harness = replace_once(
    harness,
    '''    "--controlled-failure",\n    "--synthetic-publish",\n''',
    '''    "--controlled-failure",\n    "--identity-probe",\n    "controlled-failure-checksums.log",\n    "identity-probe-checksums.log",\n    "CANDIDATE_HEAD_SHA",\n    "CANDIDATE_HEAD_TREE",\n    "EVENT_SHA",\n    "runtime-image-identity.env",\n    "RUNTIME_IMAGE_TAG_NOT_CANDIDATE_HEAD",\n    "RUNTIME_IMAGE_REVISION_LABEL_NOT_CANDIDATE_HEAD",\n    "QEMU_BINFMT_DIGEST",\n    "BUILDKIT_DIGEST",\n    "--driver-opt \"image=$BUILDKIT_REFERENCE\"",\n    "--synthetic-publish",\n''',
    "harness required markers",
)
harness = replace_once(
    harness,
    '''    for marker in (\n        "build-1-lock-hash-set.txt",\n''',
    '''    for marker in (\n        'IMAGE="senex-h011-repro:${GITHUB_SHA:-local}"',\n        "moby/buildkit:buildx-stable-1",\n        "build-1-lock-hash-set.txt",\n''',
    "harness forbidden markers",
)
harness_path.write_text(harness, encoding="utf-8")

workflow_path = Path(".github/workflows/h011-arm64-reproducibility.yml")
workflow = workflow_path.read_text(encoding="utf-8")
workflow = replace_once(
    workflow,
    '''              'result_env_written_on_every_controlled_failure':'PASS',\n              'harness_self_test':'PASS',\n''',
    '''              'result_env_written_on_every_controlled_failure':'PASS',\n              'controlled_failure_artifact_checksums':'PASS',\n              'pr_event_candidate_identity':'PASS',\n              'harness_self_test':'PASS',\n''',
    "workflow self-test schema",
)
workflow = replace_once(
    workflow,
    '''      - name: Register ARM64 emulation\n        uses: docker/setup-qemu-action@c7c53464625b32c7a7e944ae62b3e17d2b600130\n        with:\n          platforms: arm64\n\n      - name: Configure Buildx\n        uses: docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f\n''',
    f'''      - name: Register ARM64 emulation\n        uses: docker/setup-qemu-action@c7c53464625b32c7a7e944ae62b3e17d2b600130\n        with:\n          image: docker.io/tonistiigi/binfmt@{QEMU_DIGEST}\n          platforms: arm64\n          cache-image: false\n\n      - name: Configure Buildx\n        uses: docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f\n        with:\n          driver-opts: |\n            image=docker.io/moby/buildkit@{BUILDKIT_DIGEST}\n''',
    "workflow container pins",
)
workflow = replace_once(
    workflow,
    '''      - name: Verify portable evidence checksums\n        shell: bash\n''',
    '''      - name: Verify portable evidence checksums\n        if: always()\n        shell: bash\n''',
    "workflow checksum always",
)
workflow_path.write_text(workflow, encoding="utf-8")

docs_path = Path("docs/H011_REPRODUCIBLE_RUNTIME.md")
docs = docs_path.read_text(encoding="utf-8")
section = f'''\n\n## Candidate identity and immutable build tooling\n\nThe checked-out Git commit and tree are the sole candidate identity. `EVENT_SHA`\nis recorded separately because pull-request events may expose a synthetic merge\nSHA. The local runtime image tag and OCI revision label must both equal the\nchecked-out candidate SHA; a permanent identity probe simulates differing event\nand candidate SHAs.\n\nQEMU is fixed as `docker.io/tonistiigi/binfmt@{QEMU_DIGEST}`. BuildKit is fixed\nas `docker.io/moby/buildkit@{BUILDKIT_DIGEST}` for both the setup builder and\neach independent no-cache builder. The artifact records the references, digests,\nobserved BuildKit version, image ID, tag and revision label. Portable checksum\nverification runs unconditionally after the runtime step, and the controlled\nfailure self-test proves a nonzero result still produces a verifiable checksummed\nartifact without replacing its original failure reason.\n'''
if "## Candidate identity and immutable build tooling" not in docs:
    docs_path.write_text(docs.rstrip() + section, encoding="utf-8")
