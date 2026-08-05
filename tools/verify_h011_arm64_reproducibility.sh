#!/usr/bin/env bash
set -Eeuo pipefail

EVIDENCE="${1:-${GITHUB_WORKSPACE:-$PWD}/evidence}"
ROOT="$(git rev-parse --show-toplevel)"
WORK="${RUNNER_TEMP:-/tmp}/senex-h011-arm64-repro-${GITHUB_RUN_ID:-local}-$$"
EXPECTED_BASE_INDEX='sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7'
EXPECTED_BASE_ARM64='sha256:20eadabc42589e6543b24a64ab305b9895e9fcf6dbb2cadb14812f394ecdbadf'
IMAGE="senex-h011-repro:${GITHUB_SHA:-local}"
CONTAINER="senex-h011-repro-${GITHUB_RUN_ID:-local}"
RESULTS="$WORK/results"
CONTEXT="$WORK/context"
BASE_URL='http://127.0.0.1:8080'
mkdir -p "$EVIDENCE" "$WORK" "$RESULTS" "$CONTEXT"
FAILURES=()
fail(){ FAILURES+=("$1"); printf 'FAIL %s\n' "$1" >&2; }
cleanup(){
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  for b in senex-repro-1-${GITHUB_RUN_ID:-local} senex-repro-2-${GITHUB_RUN_ID:-local}; do
    docker buildx rm "$b" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

HEAD_SHA=$(git rev-parse HEAD)
HEAD_TREE=$(git rev-parse HEAD^{tree})
SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD)
printf '%s\n' "$HEAD_SHA" > "$EVIDENCE/head-sha.txt"
printf '%s\n' "$HEAD_TREE" > "$EVIDENCE/head-tree.txt"
printf '%s\n' "$SOURCE_DATE_EPOCH" > "$EVIDENCE/source-date-epoch.txt"

python3 "$ROOT/tools/verify_h011_dependency_lock.py" \
  --runtime "$ROOT/polymarket/requirements-h011-v3.txt" \
  --test "$ROOT/polymarket/requirements-h011-v3-test.txt" \
  --output "$EVIDENCE/dependency-lock.json" > "$EVIDENCE/dependency-lock.stdout"
python3 "$ROOT/tools/verify_paper_only_repository.py" \
  --repo-root "$ROOT" --output "$EVIDENCE/paper-only-repository.json" \
  > "$EVIDENCE/paper-only.stdout"

docker buildx imagetools inspect python:3.11-slim --raw > "$EVIDENCE/base-index.json"
BASE_INDEX="sha256:$(sha256sum "$EVIDENCE/base-index.json" | awk '{print $1}')"
BASE_ARM64=$(jq -r '[.manifests[] | select(.platform.os=="linux" and .platform.architecture=="arm64") | .digest][0]' "$EVIDENCE/base-index.json")
printf '%s\n' "$BASE_INDEX" > "$EVIDENCE/base-index-digest.txt"
printf '%s\n' "$BASE_ARM64" > "$EVIDENCE/base-arm64-child-digest.txt"
[[ "$BASE_INDEX" == "$EXPECTED_BASE_INDEX" ]] || fail BASE_INDEX_DIGEST_CONTRADICTION
[[ "$BASE_ARM64" == "$EXPECTED_BASE_ARM64" ]] || fail BASE_ARM64_CHILD_DIGEST_CONTRADICTION

download_lock(){
  local lock=$1 platform=$2 dest=$3 log=$4
  rm -rf "$dest" && mkdir -p "$dest"
  python3 -m pip download --disable-pip-version-check \
    --require-hashes --only-binary=:all: --no-deps \
    --platform "$platform" --implementation cp --python-version 311 --abi cp311 \
    -r "$lock" -d "$dest" > "$log" 2>&1
  find "$dest" -type f -name '*.whl' -print0 | sort -z | xargs -0 sha256sum > "$dest/SHA256SUMS"
}
for platform in manylinux2014_aarch64 manylinux2014_x86_64; do
  download_lock "$ROOT/polymarket/requirements-h011-v3.txt" "$platform" "$WORK/runtime-$platform" "$EVIDENCE/runtime-$platform.log" || fail "RUNTIME_WHEEL_RESOLUTION_${platform}_FAILED"
  download_lock "$ROOT/polymarket/requirements-h011-v3-test.txt" "$platform" "$WORK/test-$platform" "$EVIDENCE/test-$platform.log" || fail "TEST_WHEEL_RESOLUTION_${platform}_FAILED"
done

git archive --format=tar --mtime="@${SOURCE_DATE_EPOCH}" HEAD > "$WORK/context.tar"
tar -xf "$WORK/context.tar" -C "$CONTEXT"
sha256sum "$WORK/context.tar" > "$EVIDENCE/context-tar.sha256"

inspect_oci(){
  local archive=$1 prefix=$2 dir="$WORK/$prefix"
  mkdir -p "$dir"
  tar -xf "$archive" -C "$dir"
  local manifest config manifest_file config_file
  manifest=$(jq -r '.manifests[0].digest' "$dir/index.json")
  manifest_file="$dir/blobs/sha256/${manifest#sha256:}"
  config=$(jq -r '.config.digest' "$manifest_file")
  config_file="$dir/blobs/sha256/${config#sha256:}"
  jq -S . "$dir/index.json" > "$EVIDENCE/$prefix-index.json"
  jq -S . "$manifest_file" > "$EVIDENCE/$prefix-manifest.json"
  jq -S . "$config_file" > "$EVIDENCE/$prefix-config.json"
  jq -r '.layers[].digest' "$manifest_file" > "$EVIDENCE/$prefix-layer-digests.txt"
  jq -r '.rootfs.diff_ids[]' "$config_file" > "$EVIDENCE/$prefix-rootfs-diff-ids.txt"
  sha256sum "$EVIDENCE/$prefix-rootfs-diff-ids.txt" | awk '{print "sha256:"$1}' > "$EVIDENCE/$prefix-rootfs-hash.txt"
  printf '%s\n' "$manifest" > "$EVIDENCE/$prefix-manifest-digest.txt"
  printf '%s\n' "$config" > "$EVIDENCE/$prefix-config-digest.txt"
}

build_once(){
  local n=$1 builder="senex-repro-${n}-${GITHUB_RUN_ID:-local}" archive="$WORK/build-${n}.oci.tar"
  docker buildx create --name "$builder" --driver docker-container --use >/dev/null
  docker buildx inspect --builder "$builder" --bootstrap > "$EVIDENCE/build-${n}-builder.txt"
  docker buildx build --builder "$builder" --no-cache --platform linux/arm64 \
    --provenance=false --sbom=false \
    --build-arg SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
    --build-arg SENEX_CODE_SHA="$HEAD_SHA" \
    --output "type=oci,dest=$archive,rewrite-timestamp=true" \
    -f "$CONTEXT/polymarket/Dockerfile.h011-v3" "$CONTEXT" \
    > "$EVIDENCE/build-${n}.stdout" 2> "$EVIDENCE/build-${n}.stderr"
  inspect_oci "$archive" "build-$n"
  docker buildx rm "$builder" >/dev/null
}

build_once 1 || fail ARM64_BUILD_1_FAILED
build_once 2 || fail ARM64_BUILD_2_FAILED
for field in manifest-digest config-digest layer-digests rootfs-hash; do
  cmp -s "$EVIDENCE/build-1-${field}.txt" "$EVIDENCE/build-2-${field}.txt" || fail "ARM64_${field^^}_DIFFER"
done

docker buildx build --no-cache --platform linux/arm64 --provenance=false --sbom=false --load \
  --build-arg SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
  --build-arg SENEX_CODE_SHA="$HEAD_SHA" \
  -t "$IMAGE" -f "$CONTEXT/polymarket/Dockerfile.h011-v3" "$CONTEXT" \
  > "$EVIDENCE/runtime-build.stdout" 2> "$EVIDENCE/runtime-build.stderr" || fail ARM64_RUNTIME_BUILD_FAILED

if [[ ! " ${FAILURES[*]} " =~ " ARM64_RUNTIME_BUILD_FAILED " ]]; then
  docker image inspect "$IMAGE" | jq -S . > "$EVIDENCE/runtime-image-inspect.json"
  [[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}/{{.Os}}')" == 'arm64/linux' ]] || fail RUNTIME_PLATFORM_MISMATCH
  docker run -d --platform linux/arm64 --name "$CONTAINER" \
    -p 127.0.0.1:8080:8080 \
    -e H011_ORDERS_ENABLED=false \
    -e H011_RESULTS_DIR=/app/polymarket/results \
    -e SENECIO_CODE_SHA="$HEAD_SHA" \
    -e PORT=8080 \
    -v "$RESULTS:/app/polymarket/results" "$IMAGE" > "$EVIDENCE/docker-run.txt"
  docker port "$CONTAINER" > "$EVIDENCE/docker-port.txt"
  ss -ltnp > "$EVIDENCE/listeners.txt" || true
  grep -Eq '^8080/tcp -> 127\.0\.0\.1:8080$' "$EVIDENCE/docker-port.txt" || fail LOOPBACK_BIND_FAILED
  ! grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\[::\]):8080([[:space:]]|$)' "$EVIDENCE/listeners.txt" || fail PUBLIC_8080_LISTENER
  deadline=$((SECONDS+1200)); ready=false
  while (( SECONDS < deadline )); do
    if curl -fsS "$BASE_URL/readyz" | jq -e '.ok==true and .readiness==true and .runtime_state=="RUNNING"' >/dev/null 2>&1 \
       && curl -fsS "$BASE_URL/api/v3/integrity" | jq -e --arg sha "$HEAD_SHA" '.code_sha==$sha and .paper_only==true and .orders_enabled==false and .live_capital_locked==true and .chain_verified==true and .replay_verified==true and .file_sha256_matches==true and .readiness==true' >/dev/null 2>&1; then
      ready=true; break
    fi
    sleep 10
  done
  [[ "$ready" == true ]] || fail INITIAL_ENDPOINT_GATE_FAILED
  for ep in livez readyz healthz api/v3/state api/v3/integrity api/v3/replay; do
    name=${ep//\//_}
    curl -fsS "$BASE_URL/$ep" | jq -S . > "$EVIDENCE/$name.json" || fail "ENDPOINT_${name}_FAILED"
  done
  cp "$EVIDENCE/api_v3_integrity.json" "$EVIDENCE/integrity-before.json"
  cp "$EVIDENCE/api_v3_replay.json" "$EVIDENCE/replay-before.json"
  pre_seq=$(jq -r '.raw_chain.current_sequence' "$EVIDENCE/integrity-before.json")
  pre_artifact=$(jq -r '.raw_chain.artifact_name' "$EVIDENCE/integrity-before.json")
  pre_sha=$(jq -r '.raw_chain.artifact_sha256' "$EVIDENCE/integrity-before.json")
  sha256sum "$RESULTS/h011_v3/raw_chain_v1/$pre_artifact" > "$EVIDENCE/pre-tip.sha256"
  [[ "$(awk '{print $1}' "$EVIDENCE/pre-tip.sha256")" == "$pre_sha" ]] || fail PRE_RESTART_TIP_HASH_MISMATCH
  docker stop --time 30 "$CONTAINER" > "$EVIDENCE/docker-stop.txt" || fail GRACEFUL_SHUTDOWN_COMMAND_FAILED
  docker inspect "$CONTAINER" | jq -S '.[0].State' > "$EVIDENCE/container-stopped-state.json"
  jq -e '.Running==false and .OOMKilled==false and .ExitCode==0' "$EVIDENCE/container-stopped-state.json" >/dev/null || fail GRACEFUL_SHUTDOWN_STATE_FAILED
  docker start "$CONTAINER" > "$EVIDENCE/docker-start.txt" || fail RESTART_COMMAND_FAILED
  deadline=$((SECONDS+1200)); restarted=false
  while (( SECONDS < deadline )); do
    if curl -fsS "$BASE_URL/api/v3/integrity" | jq -e '.paper_only==true and .orders_enabled==false and .live_capital_locked==true and .chain_verified==true and .replay_verified==true and .file_sha256_matches==true and .readiness==true' >/dev/null 2>&1; then restarted=true; break; fi
    sleep 10
  done
  [[ "$restarted" == true ]] || fail RESTART_ENDPOINT_GATE_FAILED
  curl -fsS "$BASE_URL/api/v3/integrity" | jq -S . > "$EVIDENCE/integrity-after.json" || true
  curl -fsS "$BASE_URL/api/v3/replay" | jq -S . > "$EVIDENCE/replay-after.json" || true
  post_seq=$(jq -r '.raw_chain.current_sequence' "$EVIDENCE/integrity-after.json")
  (( post_seq >= pre_seq )) || fail RAW_CHAIN_SEQUENCE_REGRESSION
  sha256sum "$RESULTS/h011_v3/raw_chain_v1/$pre_artifact" > "$EVIDENCE/post-tip.sha256"
  [[ "$(awk '{print $1}' "$EVIDENCE/post-tip.sha256")" == "$pre_sha" ]] || fail PREVIOUS_ARTIFACT_NOT_PRESERVED
  jq -e '.raw_complete==true and .chain_verified==true and .replay_verified==true and .file_sha256_matches==true and .error==null' "$EVIDENCE/replay-after.json" >/dev/null || fail RESTART_REPLAY_FAILED
  docker logs "$CONTAINER" > "$EVIDENCE/container.log" 2>&1 || true
fi

python3 - "$EVIDENCE" "${FAILURES[@]}" <<'PY'
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]); failures=sys.argv[2:]
def text(name):
    p=root/name
    return p.read_text().strip() if p.exists() else None
report={
  'schema_version':'senex-h011-arm64-reproducibility-v2',
  'status':'PASS' if not failures else 'FAIL',
  'head_sha':text('head-sha.txt'),'head_tree':text('head-tree.txt'),
  'source_date_epoch':text('source-date-epoch.txt'),
  'base_index_digest':text('base-index-digest.txt'),
  'base_arm64_child_digest':text('base-arm64-child-digest.txt'),
  'build_1_manifest_digest':text('build-1-manifest-digest.txt'),
  'build_2_manifest_digest':text('build-2-manifest-digest.txt'),
  'build_1_config_digest':text('build-1-config-digest.txt'),
  'build_2_config_digest':text('build-2-config-digest.txt'),
  'build_1_rootfs_hash':text('build-1-rootfs-hash.txt'),
  'build_2_rootfs_hash':text('build-2-rootfs-hash.txt'),
  'failures':failures,
  'invariants':{'paper_only':True,'orders_enabled':False,'live_capital_locked':True,'real_order_network_calls':0,'wallet_or_private_key_access':0,'real_capital_actions':0,'secret_values_observed':False},
}
(root/'result.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
(root/'result.env').write_text(f"ARM64_PREFLIGHT={report['status']}\n")
PY
find "$EVIDENCE" -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > "$EVIDENCE/SHA256SUMS"
((${#FAILURES[@]}==0))
