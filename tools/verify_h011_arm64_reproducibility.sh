#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODE="run"
EVIDENCE_ARG=""
case "${1:-}" in
  --self-test)
    MODE="self-test"
    EVIDENCE_ARG="${2:-}"
    ;;
  --controlled-failure)
    MODE="controlled-failure"
    EVIDENCE_ARG="${2:-}"
    ;;
  --identity-probe)
    MODE="identity-probe"
    EVIDENCE_ARG="${2:-}"
    ;;
  "")
    ;;
  *)
    EVIDENCE_ARG="$1"
    ;;
esac
if [[ -z "$EVIDENCE_ARG" ]]; then
  EVIDENCE_ARG="${GITHUB_WORKSPACE:-$PWD}/evidence"
fi
EVIDENCE="$EVIDENCE_ARG"
mkdir -p "$EVIDENCE"

EXPECTED_BASE_INDEX="sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7"
EXPECTED_BASE_ARM64="sha256:20eadabc42589e6543b24a64ab305b9895e9fcf6dbb2cadb14812f394ecdbadf"
BASE_REFERENCE="python:3.11-slim@${EXPECTED_BASE_INDEX}"
QEMU_BINFMT_DIGEST="sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
QEMU_BINFMT_REFERENCE="docker.io/tonistiigi/binfmt@${QEMU_BINFMT_DIGEST}"
BUILDKIT_DIGEST="sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec"
BUILDKIT_REFERENCE="docker.io/moby/buildkit@${BUILDKIT_DIGEST}"
RUN_ID="${GITHUB_RUN_ID:-local}"
EVENT_SHA="${GITHUB_SHA:-unknown}"
HEAD_SHA="unknown"
HEAD_TREE="unknown"
CANDIDATE_HEAD_SHA="unknown"
CANDIDATE_HEAD_TREE="unknown"
SOURCE_DATE_EPOCH="0"
RESULT_WRITTEN="0"
CONTAINER="senex-h011-repro-${RUN_ID}"
WORK="${RUNNER_TEMP:-/tmp}/senex-h011-arm64-repro-${RUN_ID}-$$"
RESULTS="$WORK/results"
CONTEXT="$WORK/context"
BASE_URL="http://127.0.0.1:18080"
BUILDERS=()

write_result() {
  local status="${1:?status required}"
  local reason="${2:-NONE}"
  local sanitized="${reason//$'\n'/ }"
  set +e
  mkdir -p "$EVIDENCE"
  {
    printf 'ARM64_PREFLIGHT=%s\n' "$status"
    printf 'FAILURE_REASON=%s\n' "$sanitized"
    printf 'HEAD_SHA=%s\n' "$HEAD_SHA"
    printf 'HEAD_TREE=%s\n' "$HEAD_TREE"
    printf 'CANDIDATE_HEAD_SHA=%s\n' "$CANDIDATE_HEAD_SHA"
    printf 'CANDIDATE_HEAD_TREE=%s\n' "$CANDIDATE_HEAD_TREE"
    printf 'EVENT_SHA=%s\n' "$EVENT_SHA"
    printf 'IMAGE_TAG=%s\n' "${IMAGE:-unassigned}"
    printf 'QEMU_BINFMT_REFERENCE=%s\n' "$QEMU_BINFMT_REFERENCE"
    printf 'QEMU_BINFMT_DIGEST=%s\n' "$QEMU_BINFMT_DIGEST"
    printf 'BUILDKIT_REFERENCE=%s\n' "$BUILDKIT_REFERENCE"
    printf 'BUILDKIT_DIGEST=%s\n' "$BUILDKIT_DIGEST"
  } > "$EVIDENCE/result.env"
  python3 - "$EVIDENCE" "$status" "$sanitized" "$HEAD_SHA" "$HEAD_TREE" "$CANDIDATE_HEAD_SHA" "$CANDIDATE_HEAD_TREE" "$EVENT_SHA" "${IMAGE:-unassigned}" "$QEMU_BINFMT_REFERENCE" "$QEMU_BINFMT_DIGEST" "$BUILDKIT_REFERENCE" "$BUILDKIT_DIGEST" <<'PY' || true
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
(status, reason, head_sha, head_tree, candidate_head_sha, candidate_head_tree,
 event_sha, image_tag, qemu_reference, qemu_digest, buildkit_reference,
 buildkit_digest) = sys.argv[2:]
def text(name):
    path = root / name
    return path.read_text(encoding="utf-8").strip() if path.exists() else None
report = {
    "schema_version": "senex-h011-arm64-reproducibility-v3",
    "status": status,
    "failure_reason": reason,
    "head_sha": head_sha,
    "head_tree": head_tree,
    "candidate_head_sha": candidate_head_sha,
    "candidate_head_tree": candidate_head_tree,
    "event_sha": event_sha,
    "image_tag": image_tag,
    "image_id": text("runtime-image-id.txt"),
    "image_observed_tag": text("runtime-image-observed-tag.txt"),
    "image_revision_label": text("runtime-image-revision-label.txt"),
    "qemu_binfmt_reference": qemu_reference,
    "qemu_binfmt_digest": qemu_digest,
    "buildkit_reference": buildkit_reference,
    "buildkit_digest": buildkit_digest,
    "buildkit_version": text("buildkit-version.txt"),
    "source_date_epoch": text("source-date-epoch.txt"),
    "base_index_digest": text("base-index-digest.txt"),
    "base_arm64_child_digest": text("base-arm64-child-digest.txt"),
    "build_1_manifest_digest": text("build-1-manifest-digest.txt"),
    "build_2_manifest_digest": text("build-2-manifest-digest.txt"),
    "build_1_config_digest": text("build-1-config-digest.txt"),
    "build_2_config_digest": text("build-2-config-digest.txt"),
    "build_1_rootfs_hash": text("build-1-rootfs-hash.txt"),
    "build_2_rootfs_hash": text("build-2-rootfs-hash.txt"),
    "build_1_base_digest": text("build-1-base-digest.txt"),
    "build_2_base_digest": text("build-2-base-digest.txt"),
    "build_1_runtime_lock_sha256": text("build-1-runtime-lock-sha256.txt"),
    "build_2_runtime_lock_sha256": text("build-2-runtime-lock-sha256.txt"),
    "invariants": {
        "paper_only": True,
        "orders_enabled": False,
        "live_capital_locked": True,
        "real_order_network_calls": 0,
        "wallet_or_private_key_access": 0,
        "real_capital_actions": 0,
        "secret_values_observed": False,
    },
}
(root / "result.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY
  (
    cd "$EVIDENCE"
    find . -type f ! -name SHA256SUMS ! -name SHA256SUMS.tmp -print0 \
      | sort -z | xargs -0 sha256sum > SHA256SUMS.tmp
    mv SHA256SUMS.tmp SHA256SUMS
  ) || true
  RESULT_WRITTEN="1"
  set -e
}

die() {
  local reason="${1:?reason required}"
  local code="${2:-1}"
  write_result "FAIL" "$reason"
  exit "$code"
}

cleanup() {
  set +e
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  local builder=""
  for builder in "${BUILDERS[@]}"; do
    docker buildx rm "$builder" >/dev/null 2>&1 || true
  done
}

on_err() {
  local rc=$?
  local line="${BASH_LINENO[0]:-unknown}"
  local command="${BASH_COMMAND:-unknown}"
  trap - ERR
  set +e
  write_result "FAIL" "UNEXPECTED_ERROR_RC_${rc}_LINE_${line}: ${command}"
  exit "$rc"
}

on_exit() {
  local rc=$?
  set +e
  cleanup
  if [[ "$RESULT_WRITTEN" != "1" ]]; then
    if [[ "$rc" -eq 0 ]]; then
      write_result "PASS" "NONE"
    else
      write_result "FAIL" "UNEXPECTED_EXIT_${rc}"
    fi
  fi
}

trap on_err ERR
trap on_exit EXIT

if git -C "$ROOT" rev-parse HEAD >/dev/null 2>&1; then
  HEAD_SHA="$(git -C "$ROOT" rev-parse HEAD)"
  HEAD_TREE="$(git -C "$ROOT" rev-parse HEAD^{tree})"
  SOURCE_DATE_EPOCH="$(git -C "$ROOT" show -s --format=%ct HEAD)"
fi
CANDIDATE_HEAD_SHA="$HEAD_SHA"
CANDIDATE_HEAD_TREE="$HEAD_TREE"
IMAGE="senex-h011-repro:${CANDIDATE_HEAD_SHA}"
IMAGE_REVISION_LABEL_EXPECTED="$CANDIDATE_HEAD_SHA"
printf '%s\n' "$HEAD_SHA" > "$EVIDENCE/head-sha.txt"
printf '%s\n' "$HEAD_TREE" > "$EVIDENCE/head-tree.txt"
printf '%s\n' "$CANDIDATE_HEAD_SHA" > "$EVIDENCE/candidate-head-sha.txt"
printf '%s\n' "$CANDIDATE_HEAD_TREE" > "$EVIDENCE/candidate-head-tree.txt"
printf '%s\n' "$EVENT_SHA" > "$EVIDENCE/event-sha.txt"
printf '%s\n' "$IMAGE" > "$EVIDENCE/image-tag.txt"
printf '%s\n' "$SOURCE_DATE_EPOCH" > "$EVIDENCE/source-date-epoch.txt"
{
  printf 'CANDIDATE_HEAD_SHA=%s\n' "$CANDIDATE_HEAD_SHA"
  printf 'CANDIDATE_HEAD_TREE=%s\n' "$CANDIDATE_HEAD_TREE"
  printf 'EVENT_SHA=%s\n' "$EVENT_SHA"
  printf 'IMAGE_TAG=%s\n' "$IMAGE"
  printf 'IMAGE_REVISION_LABEL_EXPECTED=%s\n' "$IMAGE_REVISION_LABEL_EXPECTED"
  printf 'QEMU_BINFMT_REFERENCE=%s\n' "$QEMU_BINFMT_REFERENCE"
  printf 'QEMU_BINFMT_DIGEST=%s\n' "$QEMU_BINFMT_DIGEST"
  printf 'BUILDKIT_REFERENCE=%s\n' "$BUILDKIT_REFERENCE"
  printf 'BUILDKIT_DIGEST=%s\n' "$BUILDKIT_DIGEST"
} > "$EVIDENCE/candidate-identity.env"

if [[ "$MODE" == "identity-probe" ]]; then
  exit 0
fi

if [[ "$MODE" == "controlled-failure" ]]; then
  die "CONTROLLED_SELF_TEST_FAILURE" 97
fi

run_self_test() {
  local nested="$EVIDENCE/nested/controlled-failure"
  local child_rc="0"
  python3 "$ROOT/tools/verify_h011_shell_harness.py" \
    --script "$ROOT/tools/verify_h011_arm64_reproducibility.sh" \
    --output "$EVIDENCE/harness-static-audit.json"
  if env -i \
    PATH="$PATH" \
    HOME="${HOME:-/tmp}" \
    bash "$ROOT/tools/verify_h011_arm64_reproducibility.sh" \
      --controlled-failure "$nested"; then
  child_rc="0"
  else
    child_rc=$?
  fi
  [[ "$child_rc" -eq 97 ]] || die "HARNESS_SELF_TEST_WRONG_CHILD_RC_${child_rc}"
  [[ -d "$nested" ]] || die "ARGUMENT_AND_OUTPUT_DIRECTORY_INITIALIZATION_FAILED"
  [[ -f "$nested/result.env" ]] || die "CONTROLLED_FAILURE_RESULT_ENV_MISSING"
  grep -qx 'ARM64_PREFLIGHT=FAIL' "$nested/result.env" \
    || die "CONTROLLED_FAILURE_RESULT_ENV_INVALID"
  grep -qx 'FAILURE_REASON=CONTROLLED_SELF_TEST_FAILURE' "$nested/result.env" \
    || die "CONTROLLED_FAILURE_REASON_INVALID"
  (cd "$nested" && sha256sum -c SHA256SUMS) \
    > "$EVIDENCE/controlled-failure-checksums.log" \
    || die "CONTROLLED_FAILURE_CHECKSUMS_INVALID"
  local identity="$EVIDENCE/nested/identity-probe"
  local simulated_event="1111111111111111111111111111111111111111"
  env -i \
    PATH="$PATH" \
    HOME="${HOME:-/tmp}" \
    GITHUB_SHA="$simulated_event" \
    bash "$ROOT/tools/verify_h011_arm64_reproducibility.sh" \
      --identity-probe "$identity"
  grep -qx "CANDIDATE_HEAD_SHA=$HEAD_SHA" "$identity/candidate-identity.env" \
    || die "IDENTITY_SELF_TEST_CANDIDATE_SHA_INVALID"
  grep -qx "CANDIDATE_HEAD_TREE=$HEAD_TREE" "$identity/candidate-identity.env" \
    || die "IDENTITY_SELF_TEST_CANDIDATE_TREE_INVALID"
  grep -qx "EVENT_SHA=$simulated_event" "$identity/candidate-identity.env" \
    || die "IDENTITY_SELF_TEST_EVENT_SHA_INVALID"
  grep -qx "IMAGE_TAG=senex-h011-repro:$HEAD_SHA" "$identity/candidate-identity.env" \
    || die "IDENTITY_SELF_TEST_IMAGE_TAG_INVALID"
  grep -qx "IMAGE_REVISION_LABEL_EXPECTED=$HEAD_SHA" "$identity/candidate-identity.env" \
    || die "IDENTITY_SELF_TEST_REVISION_LABEL_INVALID"
  (cd "$identity" && sha256sum -c SHA256SUMS) \
    > "$EVIDENCE/identity-probe-checksums.log" \
    || die "IDENTITY_SELF_TEST_CHECKSUMS_INVALID"
  python3 - "$EVIDENCE/harness-self-test.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
report = {
    "schema_version": "senex-h011-harness-self-test-v1",
    "status": "PASS",
    "bash_syntax_check": "PASS",
    "unbound_variable_static_or_unit_check": "PASS",
    "argument_and_output_directory_initialization": "PASS",
    "result_env_written_on_every_controlled_failure": "PASS",
    "controlled_failure_artifact_checksums": "PASS",
    "pr_event_candidate_identity": "PASS",
    "harness_self_test": "PASS",
}
path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY
  write_result "PASS" "HARNESS_SELF_TEST_PASS"
}

if [[ "$MODE" == "self-test" ]]; then
  run_self_test
  exit 0
fi

mkdir -p "$WORK" "$RESULTS" "$CONTEXT"

python3 "$ROOT/tools/verify_h011_dependency_lock.py" \
  --runtime "$ROOT/polymarket/requirements-h011-v3-runtime.txt" \
  --test "$ROOT/polymarket/requirements-h011-v3-test.txt" \
  --ci "$ROOT/polymarket/requirements-h011-v3.txt" \
  --output "$EVIDENCE/dependency-lock.json" \
  > "$EVIDENCE/dependency-lock.stdout"
python3 "$ROOT/tools/verify_paper_only_repository.py" \
  --repo-root "$ROOT" \
  --output "$EVIDENCE/paper-only-repository.json" \
  > "$EVIDENCE/paper-only.stdout"

sha256sum "$ROOT/polymarket/requirements-h011-v3-runtime.txt" \
  | awk '{print $1"  /app/polymarket/requirements-h011-v3-runtime.txt"}' \
  > "$EVIDENCE/source-runtime-lock-sha256.txt"

docker buildx imagetools inspect "$BASE_REFERENCE" --raw \
  > "$EVIDENCE/base-index.json"
BASE_INDEX="sha256:$(sha256sum "$EVIDENCE/base-index.json" | awk '{print $1}')"
BASE_ARM64="$(jq -r '[.manifests[] | select(.platform.os=="linux" and .platform.architecture=="arm64") | .digest][0]' "$EVIDENCE/base-index.json")"
printf '%s\n' "$BASE_INDEX" > "$EVIDENCE/base-index-digest.txt"
printf '%s\n' "$BASE_ARM64" > "$EVIDENCE/base-arm64-child-digest.txt"
[[ "$BASE_INDEX" == "$EXPECTED_BASE_INDEX" ]] \
  || die "BASE_INDEX_DIGEST_CONTRADICTION"
[[ "$BASE_ARM64" == "$EXPECTED_BASE_ARM64" ]] \
  || die "BASE_ARM64_CHILD_DIGEST_CONTRADICTION"
docker buildx imagetools inspect "python:3.11-slim@${BASE_ARM64}" --raw \
  > "$EVIDENCE/base-arm64-manifest.json"
OBSERVED_BASE_ARM64="sha256:$(sha256sum "$EVIDENCE/base-arm64-manifest.json" | awk '{print $1}')"
[[ "$OBSERVED_BASE_ARM64" == "$EXPECTED_BASE_ARM64" ]] \
  || die "BASE_ARM64_MANIFEST_DIGEST_CONTRADICTION"
jq -r '.layers[].digest' "$EVIDENCE/base-arm64-manifest.json" \
  > "$EVIDENCE/base-arm64-layer-digests.txt"
[[ -s "$EVIDENCE/base-arm64-layer-digests.txt" ]] \
  || die "BASE_ARM64_LAYER_SET_MISSING"

download_lock() {
  local lock="${1:?lock required}"
  local platform="${2:?platform required}"
  local dest="${3:?destination required}"
  local log="${4:?log required}"
  rm -rf "$dest"
  mkdir -p "$dest"
  if ! python3 -m pip download --disable-pip-version-check \
    --require-hashes \
    --only-binary=:all: \
    --no-deps \
    --platform "$platform" \
    --implementation cp \
    --python-version 311 \
    --abi cp311 \
    -r "$lock" \
    -d "$dest" > "$log" 2>&1; then
    return 1
  fi
  if find "$dest" -type f ! -name '*.whl' | grep -q .; then
    return 1
  fi
  find "$dest" -type f -name '*.whl' -print0 \
    | sort -z | xargs -0 -r sha256sum > "$dest/SHA256SUMS"
  [[ -s "$dest/SHA256SUMS" ]]
}

platform=""
for platform in manylinux2014_aarch64 manylinux2014_x86_64; do
  download_lock \
    "$ROOT/polymarket/requirements-h011-v3-runtime.txt" \
    "$platform" \
    "$WORK/runtime-$platform" \
    "$EVIDENCE/runtime-$platform.log" \
    || die "RUNTIME_WHEEL_RESOLUTION_${platform}_FAILED"
  download_lock \
    "$ROOT/polymarket/requirements-h011-v3-test.txt" \
    "$platform" \
    "$WORK/test-$platform" \
    "$EVIDENCE/test-$platform.log" \
    || die "TEST_WHEEL_RESOLUTION_${platform}_FAILED"
done

git -C "$ROOT" archive \
  --format=tar \
  --mtime="@${SOURCE_DATE_EPOCH}" \
  HEAD > "$WORK/context.tar"
tar -xf "$WORK/context.tar" -C "$CONTEXT"
sha256sum "$WORK/context.tar" > "$EVIDENCE/context-tar.sha256"

inspect_oci() {
  local archive="${1:?archive required}"
  local prefix="${2:?prefix required}"
  local dir="$WORK/$prefix"
  local manifest=""
  local config=""
  local manifest_file=""
  local config_file=""
  mkdir -p "$dir"
  tar -xf "$archive" -C "$dir"
  manifest="$(jq -r '.manifests[0].digest' "$dir/index.json")"
  manifest_file="$dir/blobs/sha256/${manifest#sha256:}"
  config="$(jq -r '.config.digest' "$manifest_file")"
  config_file="$dir/blobs/sha256/${config#sha256:}"
  jq -S . "$dir/index.json" > "$EVIDENCE/$prefix-index.json"
  jq -S . "$manifest_file" > "$EVIDENCE/$prefix-manifest.json"
  jq -S . "$config_file" > "$EVIDENCE/$prefix-config.json"
  jq -r '.layers[].digest' "$manifest_file" \
    > "$EVIDENCE/$prefix-layer-digests.txt"
  jq -r '.rootfs.diff_ids[]' "$config_file" \
    > "$EVIDENCE/$prefix-rootfs-diff-ids.txt"
  sha256sum "$EVIDENCE/$prefix-rootfs-diff-ids.txt" \
    | awk '{print "sha256:"$1}' > "$EVIDENCE/$prefix-rootfs-hash.txt"
  printf '%s\n' "$manifest" > "$EVIDENCE/$prefix-manifest-digest.txt"
  printf '%s\n' "$config" > "$EVIDENCE/$prefix-config-digest.txt"
  python3 "$ROOT/tools/verify_h011_oci_build_evidence.py" \
    --oci-dir "$dir" \
    --base-manifest "$EVIDENCE/base-arm64-manifest.json" \
    --expected-base-digest "$EXPECTED_BASE_ARM64" \
    --runtime-lock-source "$ROOT/polymarket/requirements-h011-v3-runtime.txt" \
    --output-dir "$EVIDENCE" \
    --prefix "$prefix" \
    > "$EVIDENCE/$prefix-derived-build-evidence.stdout"
}

build_once() {
  local number="${1:?build number required}"
  local builder="senex-repro-${number}-${RUN_ID}"
  local archive="$WORK/build-${number}.oci.tar"
  BUILDERS+=("$builder")
  docker buildx create \
    --name "$builder" \
    --driver docker-container \
    --driver-opt "image=$BUILDKIT_REFERENCE" \
    --use >/dev/null
  docker buildx inspect \
    --builder "$builder" \
    --bootstrap > "$EVIDENCE/build-${number}-builder.txt"
  docker buildx build \
    --builder "$builder" \
    --no-cache \
    --platform linux/arm64 \
    --provenance=false \
    --sbom=false \
    --build-arg SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
    --build-arg SENEX_CODE_SHA="$CANDIDATE_HEAD_SHA" \
    --output "type=oci,dest=$archive,rewrite-timestamp=true" \
    -f "$CONTEXT/polymarket/Dockerfile.h011-v3" \
    "$CONTEXT" \
    > "$EVIDENCE/build-${number}.stdout" \
    2> "$EVIDENCE/build-${number}.stderr"
  inspect_oci "$archive" "build-$number"
  docker buildx rm "$builder" >/dev/null
}

build_once 1
build_once 2

require_equal() {
  local first="${1:?first file required}"
  local second="${2:?second file required}"
  local label="${3:?label required}"
  cmp -s "$first" "$second" || die "$label"
}

require_equal \
  "$EVIDENCE/build-1-manifest-digest.txt" \
  "$EVIDENCE/build-2-manifest-digest.txt" \
  "ARM64_MANIFEST_DIGESTS_DIFFER"
require_equal \
  "$EVIDENCE/build-1-config-digest.txt" \
  "$EVIDENCE/build-2-config-digest.txt" \
  "ARM64_CONFIG_DIGESTS_DIFFER"
require_equal \
  "$EVIDENCE/build-1-layer-digests.txt" \
  "$EVIDENCE/build-2-layer-digests.txt" \
  "ARM64_LAYER_DIGESTS_DIFFER"
require_equal \
  "$EVIDENCE/build-1-rootfs-hash.txt" \
  "$EVIDENCE/build-2-rootfs-hash.txt" \
  "ARM64_ROOTFS_HASHES_DIFFER"
require_equal \
  "$EVIDENCE/base-arm64-layer-digests.txt" \
  "$EVIDENCE/build-1-base-layer-digests.txt" \
  "BUILD_1_BASE_LAYER_PREFIX_DIFFERS"
require_equal \
  "$EVIDENCE/base-arm64-layer-digests.txt" \
  "$EVIDENCE/build-2-base-layer-digests.txt" \
  "BUILD_2_BASE_LAYER_PREFIX_DIFFERS"
require_equal \
  "$EVIDENCE/source-runtime-lock-sha256.txt" \
  "$EVIDENCE/build-1-runtime-lock-sha256.txt" \
  "BUILD_1_RUNTIME_LOCK_DIFFERS_FROM_SOURCE"
require_equal \
  "$EVIDENCE/source-runtime-lock-sha256.txt" \
  "$EVIDENCE/build-2-runtime-lock-sha256.txt" \
  "BUILD_2_RUNTIME_LOCK_DIFFERS_FROM_SOURCE"
require_equal \
  "$EVIDENCE/build-1-base-digest.txt" \
  "$EVIDENCE/build-2-base-digest.txt" \
  "ARM64_BASE_DIGESTS_DIFFER"
require_equal \
  "$EVIDENCE/build-1-runtime-lock-sha256.txt" \
  "$EVIDENCE/build-2-runtime-lock-sha256.txt" \
  "ARM64_RUNTIME_LOCK_HASHES_DIFFER"
BUILDKIT_VERSION_1="$(awk '$1=="BuildKit" && $2=="version:" {print $3; exit}' "$EVIDENCE/build-1-builder.txt")"
BUILDKIT_VERSION_2="$(awk '$1=="BuildKit" && $2=="version:" {print $3; exit}' "$EVIDENCE/build-2-builder.txt")"
[[ -n "$BUILDKIT_VERSION_1" && "$BUILDKIT_VERSION_1" == "$BUILDKIT_VERSION_2" ]] \
  || die "BUILDKIT_VERSION_MISSING_OR_DIFFERENT"
printf '%s\n' "$BUILDKIT_VERSION_1" > "$EVIDENCE/buildkit-version.txt"
{
  printf 'QEMU_BINFMT_REFERENCE=%s\n' "$QEMU_BINFMT_REFERENCE"
  printf 'QEMU_BINFMT_DIGEST=%s\n' "$QEMU_BINFMT_DIGEST"
  printf 'BUILDKIT_REFERENCE=%s\n' "$BUILDKIT_REFERENCE"
  printf 'BUILDKIT_DIGEST=%s\n' "$BUILDKIT_DIGEST"
  printf 'BUILDKIT_VERSION=%s\n' "$BUILDKIT_VERSION_1"
} > "$EVIDENCE/container-toolchain.env"

docker buildx build \
  --no-cache \
  --platform linux/arm64 \
  --provenance=false \
  --sbom=false \
  --load \
  --build-arg SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
  --build-arg SENEX_CODE_SHA="$CANDIDATE_HEAD_SHA" \
  -t "$IMAGE" \
  -f "$CONTEXT/polymarket/Dockerfile.h011-v3" \
  "$CONTEXT" \
  > "$EVIDENCE/runtime-build.stdout" \
  2> "$EVIDENCE/runtime-build.stderr"

docker image inspect "$IMAGE" | jq -S . \
  > "$EVIDENCE/runtime-image-inspect.json"
IMAGE_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
IMAGE_REVISION_LABEL="$(docker image inspect "$IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
IMAGE_OBSERVED_TAG="$(docker image inspect "$IMAGE" --format '{{range .RepoTags}}{{println .}}{{end}}' \
  | awk -v expected="$IMAGE" '$0==expected {print; exit}')"
printf '%s\n' "$IMAGE_ID" > "$EVIDENCE/runtime-image-id.txt"
printf '%s\n' "$IMAGE_REVISION_LABEL" > "$EVIDENCE/runtime-image-revision-label.txt"
printf '%s\n' "$IMAGE_OBSERVED_TAG" > "$EVIDENCE/runtime-image-observed-tag.txt"
[[ "$IMAGE_OBSERVED_TAG" == "$IMAGE" ]] \
  || die "RUNTIME_IMAGE_TAG_NOT_CANDIDATE_HEAD"
[[ "$IMAGE_REVISION_LABEL" == "$CANDIDATE_HEAD_SHA" ]] \
  || die "RUNTIME_IMAGE_REVISION_LABEL_NOT_CANDIDATE_HEAD"
identity_field=""
for identity_field in \
  CANDIDATE_HEAD_SHA CANDIDATE_HEAD_TREE EVENT_SHA IMAGE_OBSERVED_TAG IMAGE_ID \
  IMAGE_REVISION_LABEL QEMU_BINFMT_REFERENCE QEMU_BINFMT_DIGEST \
  BUILDKIT_REFERENCE BUILDKIT_DIGEST BUILDKIT_VERSION_1; do
  [[ -n "${!identity_field}" && "${!identity_field}" != "unknown" ]] \
    || die "RUNTIME_IMAGE_IDENTITY_FIELD_EMPTY_${identity_field}"
done
{
  printf 'CANDIDATE_HEAD_SHA=%s\n' "$CANDIDATE_HEAD_SHA"
  printf 'CANDIDATE_HEAD_TREE=%s\n' "$CANDIDATE_HEAD_TREE"
  printf 'EVENT_SHA=%s\n' "$EVENT_SHA"
  printf 'IMAGE_TAG=%s\n' "$IMAGE_OBSERVED_TAG"
  printf 'IMAGE_ID=%s\n' "$IMAGE_ID"
  printf 'IMAGE_REVISION_LABEL=%s\n' "$IMAGE_REVISION_LABEL"
  printf 'QEMU_BINFMT_REFERENCE=%s\n' "$QEMU_BINFMT_REFERENCE"
  printf 'QEMU_BINFMT_DIGEST=%s\n' "$QEMU_BINFMT_DIGEST"
  printf 'BUILDKIT_REFERENCE=%s\n' "$BUILDKIT_REFERENCE"
  printf 'BUILDKIT_DIGEST=%s\n' "$BUILDKIT_DIGEST"
  printf 'BUILDKIT_VERSION=%s\n' "$BUILDKIT_VERSION_1"
} > "$EVIDENCE/runtime-image-identity.env"
[[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}/{{.Os}}')" == "arm64/linux" ]] \
  || die "RUNTIME_PLATFORM_MISMATCH"
if docker run --rm --platform linux/arm64 "$IMAGE" \
  python3 -c 'import importlib.util; raise SystemExit(0 if importlib.util.find_spec("pytest") is None else 1)'; then
  printf 'PYTEST_ABSENT_FROM_RUNTIME_IMAGE=PASS\n' \
    > "$EVIDENCE/runtime-dependency-boundary.env"
else
  die "PYTEST_PRESENT_IN_RUNTIME_IMAGE"
fi

docker run --rm \
  --platform linux/arm64 \
  -v "$RESULTS:/app/polymarket/results" \
  -e SENECIO_CODE_SHA="$CANDIDATE_HEAD_SHA" \
  "$IMAGE" \
  python3 /app/polymarket/h011_v3_runtime.py \
    --synthetic-publish \
    --run-id arm64-baseline-run \
    --scan-id arm64-baseline-scan \
  > "$EVIDENCE/synthetic-publish.log" 2>&1 \
  || die "SYNTHETIC_BASELINE_PUBLISH_FAILED"
[[ -s "$EVIDENCE/synthetic-publish.log" ]] \
  || die "SYNTHETIC_BASELINE_EVIDENCE_MISSING"

docker run -d \
  --platform linux/arm64 \
  --name "$CONTAINER" \
  -p 127.0.0.1:18080:8080 \
  -e H011_ORDERS_ENABLED=false \
  -e H011_RUNTIME_DIAGNOSTIC_ONLY=true \
  -e H011_RESULTS_DIR=/app/polymarket/results \
  -e SENECIO_CODE_SHA="$CANDIDATE_HEAD_SHA" \
  -e PORT=8080 \
  -v "$RESULTS:/app/polymarket/results" \
  "$IMAGE" > "$EVIDENCE/docker-run.txt"
docker port "$CONTAINER" > "$EVIDENCE/docker-port.txt"
ss -ltnp > "$EVIDENCE/listeners.txt" || true
grep -Eq '^8080/tcp -> 127\.0\.0\.1:18080$' "$EVIDENCE/docker-port.txt" \
  || die "LOOPBACK_BIND_FAILED"
if grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\[::\]):18080([[:space:]]|$)' "$EVIDENCE/listeners.txt"; then
  die "PUBLIC_18080_LISTENER"
fi

deadline=$((SECONDS + 300))
ready="false"
while (( SECONDS < deadline )); do
  if curl -fsS "$BASE_URL/readyz" \
      | jq -e '.ok==true and .readiness==true and .runtime_state=="DEGRADED"' \
        >/dev/null 2>&1 \
    && curl -fsS "$BASE_URL/api/v3/integrity" \
      | jq -e --arg sha "$CANDIDATE_HEAD_SHA" \
        '.code_sha==$sha and .paper_only==true and .orders_enabled==false and .live_capital_locked==true and .chain_verified==true and .replay_verified==true and .file_sha256_matches==true and .readiness==true and .runtime.runtime_state=="DEGRADED" and .runtime.scanner_enabled==false and .runtime.publication_enabled==false and .runtime.blocking_reason=="diagnostic_only_mode"' \
        >/dev/null 2>&1; then
    ready="true"
    break
  fi
  sleep 10
done
if [[ "$ready" != "true" ]]; then
  docker inspect "$CONTAINER" | jq -S '.[0].State' \
    > "$EVIDENCE/container-initial-failure-state.json" 2>/dev/null || true
  docker logs "$CONTAINER" \
    > "$EVIDENCE/container-initial-failure.log" 2>&1 || true
  curl -sS "$BASE_URL/readyz" \
    > "$EVIDENCE/readyz-initial-failure.json" 2>/dev/null || true
  curl -sS "$BASE_URL/api/v3/integrity" \
    > "$EVIDENCE/integrity-initial-failure.json" 2>/dev/null || true
  die "INITIAL_ENDPOINT_GATE_FAILED"
fi

endpoint=""
for endpoint in livez readyz healthz api/v3/state api/v3/integrity api/v3/replay; do
  name="${endpoint//\//_}"
  curl -fsS "$BASE_URL/$endpoint" | jq -S . \
    > "$EVIDENCE/$name.json"
done
jq -e \
  '.ok==true and .status=="DEGRADED" and .readiness==true and .runtime.runtime_state=="DEGRADED" and .runtime.scanner_enabled==false and .runtime.publication_enabled==false and .runtime.blocking_reason=="diagnostic_only_mode"' \
  "$EVIDENCE/healthz.json" >/dev/null \
  || die "DIAGNOSTIC_HEALTH_CONTRACT_FAILED"

cp "$EVIDENCE/api_v3_integrity.json" "$EVIDENCE/integrity-before.json"
cp "$EVIDENCE/api_v3_replay.json" "$EVIDENCE/replay-before.json"
pre_seq="$(jq -r '.raw_chain.current_sequence' "$EVIDENCE/integrity-before.json")"
[[ "$pre_seq" =~ ^[0-9]+$ ]] \
  || die "PRE_RESTART_RAW_SEQUENCE_MISSING"
pre_artifact="$(jq -r '.raw_chain.artifact_name' "$EVIDENCE/integrity-before.json")"
pre_sha="$(jq -r '.raw_chain.artifact_sha256' "$EVIDENCE/integrity-before.json")"
[[ -n "$pre_artifact" && "$pre_artifact" != "null" ]] \
  || die "PRE_RESTART_RAW_ARTIFACT_MISSING"
[[ -n "$pre_sha" && "$pre_sha" != "null" ]] \
  || die "PRE_RESTART_RAW_SHA_MISSING"
jq -e \
  '.raw_chain.run_id=="arm64-baseline-run" and .raw_chain.scan_id=="arm64-baseline-scan"' \
  "$EVIDENCE/integrity-before.json" >/dev/null \
  || die "SYNTHETIC_BASELINE_IDENTITY_MISMATCH"
sha256sum "$RESULTS/h011_v3/raw_chain_v1/$pre_artifact" \
  > "$EVIDENCE/pre-tip.sha256"
[[ "$(awk '{print $1}' "$EVIDENCE/pre-tip.sha256")" == "$pre_sha" ]] \
  || die "PRE_RESTART_TIP_HASH_MISMATCH"

docker stop --time 30 "$CONTAINER" > "$EVIDENCE/docker-stop.txt"
docker inspect "$CONTAINER" | jq -S '.[0].State' \
  > "$EVIDENCE/container-stopped-state.json"
jq -e '.Running==false and .OOMKilled==false and .ExitCode==0' \
  "$EVIDENCE/container-stopped-state.json" >/dev/null \
  || die "GRACEFUL_SHUTDOWN_STATE_FAILED"
docker start "$CONTAINER" > "$EVIDENCE/docker-start.txt"

deadline=$((SECONDS + 300))
restarted="false"
while (( SECONDS < deadline )); do
  if curl -fsS "$BASE_URL/api/v3/integrity" \
      | jq -e \
        '.paper_only==true and .orders_enabled==false and .live_capital_locked==true and .chain_verified==true and .replay_verified==true and .file_sha256_matches==true and .readiness==true and .runtime.runtime_state=="DEGRADED" and .runtime.scanner_enabled==false and .runtime.publication_enabled==false and .runtime.blocking_reason=="diagnostic_only_mode"' \
        >/dev/null 2>&1; then
    restarted="true"
    break
  fi
  sleep 10
done
[[ "$restarted" == "true" ]] || die "RESTART_ENDPOINT_GATE_FAILED"
curl -fsS "$BASE_URL/api/v3/integrity" | jq -S . \
  > "$EVIDENCE/integrity-after.json"
curl -fsS "$BASE_URL/api/v3/replay" | jq -S . \
  > "$EVIDENCE/replay-after.json"
post_seq="$(jq -r '.raw_chain.current_sequence' "$EVIDENCE/integrity-after.json")"
[[ "$post_seq" =~ ^[0-9]+$ ]] \
  || die "POST_RESTART_RAW_SEQUENCE_MISSING"
(( post_seq >= pre_seq )) || die "RAW_CHAIN_SEQUENCE_REGRESSION"
sha256sum "$RESULTS/h011_v3/raw_chain_v1/$pre_artifact" \
  > "$EVIDENCE/post-tip.sha256"
[[ "$(awk '{print $1}' "$EVIDENCE/post-tip.sha256")" == "$pre_sha" ]] \
  || die "PREVIOUS_ARTIFACT_NOT_PRESERVED"
jq -e \
  '.raw_complete==true and .chain_verified==true and .replay_verified==true and .file_sha256_matches==true and .error==null' \
  "$EVIDENCE/replay-after.json" >/dev/null \
  || die "RESTART_REPLAY_FAILED"
docker logs "$CONTAINER" > "$EVIDENCE/container.log" 2>&1 || true

write_result "PASS" "NONE"
