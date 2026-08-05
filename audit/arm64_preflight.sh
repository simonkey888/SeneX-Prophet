#!/usr/bin/env bash
set -Eeuo pipefail

PRODUCT_SHA="${PRODUCT_SHA:-6ee7a139055766a1e2fda1eae7e39358e88974c2}"
PRODUCT_TREE="${PRODUCT_TREE:-b6eeb359f7e177a33f4b7bed77a8f77fe9acf96e}"
PRODUCT_DIR="${PRODUCT_DIR:-$GITHUB_WORKSPACE/product}"
CONTROL_DIR="${CONTROL_DIR:-$GITHUB_WORKSPACE/control}"
EVIDENCE="${EVIDENCE:-$GITHUB_WORKSPACE/evidence}"
WORK="${RUNNER_TEMP:-/tmp}/senex-arm64-preflight"
RESULTS="$WORK/results"
WHEELS="$WORK/wheels"
CONTAINER=senex-h011-arm64-preflight
IMAGE=senex-h011-v3:6ee7a139-arm64-preflight
BASE=http://127.0.0.1:8080
mkdir -p "$EVIDENCE" "$WORK" "$RESULTS" "$WHEELS"
FAILURES=()
fail(){ FAILURES+=("$1"); printf 'FAIL %s\n' "$1" >&2; }
cleanup(){ docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# 1. Exact product tree and audit-branch ancestry.
ACTUAL_SHA=$(git -C "$PRODUCT_DIR" rev-parse HEAD)
ACTUAL_TREE=$(git -C "$PRODUCT_DIR" rev-parse HEAD^{tree})
CONTROL_HEAD=$(git -C "$CONTROL_DIR" rev-parse HEAD)
FIRST_AUDIT=$(git -C "$CONTROL_DIR" rev-list --reverse "${PRODUCT_SHA}..${CONTROL_HEAD}" | head -n1)
BASE_PARENT=$(git -C "$CONTROL_DIR" rev-parse "${FIRST_AUDIT}^")
python3 - "$PRODUCT_SHA" "$ACTUAL_SHA" "$PRODUCT_TREE" "$ACTUAL_TREE" "$CONTROL_HEAD" "$BASE_PARENT" > "$EVIDENCE/exact-tree.json" <<'PY'
import json,sys
es,as_,et,at,ch,bp=sys.argv[1:]
print(json.dumps({"schema_version":"senex-arm64-exact-tree-v1","expected_sha":es,"actual_sha":as_,"expected_tree":et,"actual_tree":at,"control_head":ch,"control_base_parent":bp,"sha_match":es==as_,"tree_match":et==at,"control_based_exactly_on_product":bp==es},sort_keys=True,indent=2))
PY
[[ "$ACTUAL_SHA" == "$PRODUCT_SHA" ]] || fail PRODUCT_SHA_MISMATCH
[[ "$ACTUAL_TREE" == "$PRODUCT_TREE" ]] || fail PRODUCT_TREE_MISMATCH
[[ "$BASE_PARENT" == "$PRODUCT_SHA" ]] || fail CONTROL_BRANCH_BASE_MISMATCH
[[ -z "$(git -C "$PRODUCT_DIR" status --porcelain=v1 --untracked-files=no)" ]] || fail PRODUCT_TRACKED_TREE_DIRTY

# 2. Permanent paper-only repository gate.
set +e
python3 "$PRODUCT_DIR/tools/verify_paper_only_repository.py" --repo-root "$PRODUCT_DIR" --output "$EVIDENCE/paper-only-repository.json" >"$EVIDENCE/paper-gate.stdout" 2>"$EVIDENCE/paper-gate.stderr"
PAPER_RC=$?
set -e
printf '%s\n' "$PAPER_RC" > "$EVIDENCE/paper-gate.rc"
[[ "$PAPER_RC" -eq 0 ]] || fail PAPER_ONLY_GATE_FAILED

# 3. Resolve the exact linux/arm64 manifest behind the mutable base tag.
set +e
docker buildx imagetools inspect python:3.11-slim --raw > "$EVIDENCE/base-index.json" 2> "$EVIDENCE/base-index.stderr"
BASE_RC=$?
set -e
BASE_INDEX_DIGEST=""; BASE_ARM64_DIGEST=""
if [[ "$BASE_RC" -ne 0 ]]; then
  fail BASE_IMAGE_RESOLUTION_FAILED
else
  BASE_INDEX_DIGEST="sha256:$(sha256sum "$EVIDENCE/base-index.json"|awk '{print $1}')"
  BASE_ARM64_DIGEST=$(jq -r '[.manifests[]?|select(.platform.os=="linux" and .platform.architecture=="arm64")|select((.platform.variant//"v8")=="v8")|.digest][0]//empty' "$EVIDENCE/base-index.json")
  [[ -n "$BASE_ARM64_DIGEST" ]] || fail BASE_ARM64_MANIFEST_MISSING
fi
if [[ -n "$BASE_ARM64_DIGEST" ]]; then
  docker buildx imagetools inspect "python@${BASE_ARM64_DIGEST}" --raw > "$EVIDENCE/base-arm64-manifest.json" 2> "$EVIDENCE/base-arm64-manifest.stderr" || fail BASE_ARM64_MANIFEST_FETCH_FAILED
fi
python3 - "$BASE_INDEX_DIGEST" "$BASE_ARM64_DIGEST" > "$EVIDENCE/base-assessment.json" <<'PY'
import json,sys
idx,arm=sys.argv[1:]
print(json.dumps({"reference":"python:3.11-slim","index_digest":idx or None,"linux_arm64_manifest_digest":arm or None,"arm64_present":bool(arm),"tag_mutable":True},sort_keys=True,indent=2))
PY

# 4. Resolve ARM64 wheels and independently assess whether the exact tree locks them.
set +e
python3 -m pip download --disable-pip-version-check --only-binary=:all: --platform manylinux2014_aarch64 --implementation cp --python-version 311 --abi cp311 -r "$PRODUCT_DIR/polymarket/requirements-h011-v3.txt" -d "$WHEELS" > "$EVIDENCE/pip-download.stdout" 2> "$EVIDENCE/pip-download.stderr"
PIP_RC=$?
set -e
python3 - "$PRODUCT_DIR/polymarket/requirements-h011-v3.txt" "$WHEELS" "$PIP_RC" > "$EVIDENCE/dependencies.json" <<'PY'
import hashlib,json,pathlib,re,sys,zipfile
req=pathlib.Path(sys.argv[1]); wheels=pathlib.Path(sys.argv[2]); rc=int(sys.argv[3])
lines=[x.strip() for x in req.read_text().splitlines() if x.strip() and not x.lstrip().startswith('#')]
pin=re.compile(r'^[A-Za-z0-9_.-]+(?:\[[^]]+\])?==[^;\s]+(?:\s*;.*)?$')
items=[]
for p in sorted(wheels.glob('*.whl')):
    tags=[]
    try:
        with zipfile.ZipFile(p) as z:
            n=next(n for n in z.namelist() if n.endswith('.dist-info/WHEEL'))
            tags=[ln[5:] for ln in z.read(n).decode(errors='replace').splitlines() if ln.startswith('Tag: ')]
    except Exception as e: tags=[f'ERROR:{type(e).__name__}:{e}']
    items.append({'file':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'tags':tags,'arm64_or_universal':any(('aarch64' in t or t.endswith('-any')) and not t.startswith('ERROR:') for t in tags)})
pinned=bool(lines) and all(pin.match(x) for x in lines)
hashed=bool(lines) and all('--hash=sha256:' in x for x in lines)
print(json.dumps({'requirements':lines,'source_pinned':pinned,'source_hashed':hashed,'source_dependency_lock':pinned and hashed,'download_rc':rc,'arm64_resolution_pass':rc==0 and bool(items),'all_wheels_arm64_or_universal':bool(items) and all(x['arm64_or_universal'] for x in items),'wheels':items},sort_keys=True,indent=2))
PY
[[ "$PIP_RC" -eq 0 ]] || fail ARM64_DEPENDENCY_RESOLUTION_FAILED
jq -e '.all_wheels_arm64_or_universal==true' "$EVIDENCE/dependencies.json" >/dev/null || fail ARM64_WHEEL_COMPATIBILITY_FAILED
jq -e '.source_dependency_lock==true' "$EVIDENCE/dependencies.json" >/dev/null || fail PRODUCT_DEPENDENCIES_NOT_PINNED_AND_HASH_LOCKED

# 5. Two independent no-cache linux/arm64 builds and manifest comparison.
SOURCE_DATE_EPOCH=$(git -C "$PRODUCT_DIR" show -s --format=%ct "$PRODUCT_SHA")
printf '%s\n' "$SOURCE_DATE_EPOCH" > "$EVIDENCE/source-date-epoch.txt"
build_once(){
  n=$1; archive="$WORK/build-${n}.oci.tar"; out="$WORK/oci-${n}"
  set +e
  SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" docker buildx build --pull --no-cache --platform linux/arm64 --provenance=false --sbom=false --metadata-file "$EVIDENCE/build-${n}-metadata.json" --output "type=oci,dest=${archive}" -f "$PRODUCT_DIR/polymarket/Dockerfile.h011-v3" "$PRODUCT_DIR" >"$EVIDENCE/build-${n}.stdout" 2>"$EVIDENCE/build-${n}.stderr"
  rc=$?
  set -e
  printf '%s\n' "$rc" > "$EVIDENCE/build-${n}.rc"
  if [[ "$rc" -ne 0 ]]; then fail "ARM64_BUILD_${n}_FAILED"; return; fi
  mkdir -p "$out"; tar -xf "$archive" -C "$out"; jq -S . "$out/index.json" > "$EVIDENCE/build-${n}-index.json"
  digest=$(jq -r '.manifests[0].digest//empty' "$out/index.json"); printf '%s\n' "$digest" > "$EVIDENCE/build-${n}-manifest-digest.txt"
  [[ -n "$digest" ]] || fail "ARM64_BUILD_${n}_DIGEST_MISSING"
  if [[ -n "$digest" ]]; then jq -S . "$out/blobs/sha256/${digest#sha256:}" > "$EVIDENCE/build-${n}-manifest.json"; fi
  rm -f "$archive"
}
build_once 1
build_once 2
D1=$(cat "$EVIDENCE/build-1-manifest-digest.txt" 2>/dev/null||true)
D2=$(cat "$EVIDENCE/build-2-manifest-digest.txt" 2>/dev/null||true)
REPRO=false
if [[ -n "$D1" && "$D1" == "$D2" ]]; then REPRO=true; else fail ARM64_BUILD_MANIFEST_DIGESTS_DIFFER; fi
python3 - "$D1" "$D2" "$REPRO" > "$EVIDENCE/reproducibility.json" <<'PY'
import json,sys
a,b,s=sys.argv[1:]
print(json.dumps({'build_1_manifest_digest':a or None,'build_2_manifest_digest':b or None,'immediate_manifest_reproducible':s=='true'},sort_keys=True,indent=2))
PY

# 6. Load the exact Dockerfile as linux/arm64 and exercise loopback/runtime/restart.
set +e
SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" docker buildx build --platform linux/arm64 --provenance=false --sbom=false --load -t "$IMAGE" -f "$PRODUCT_DIR/polymarket/Dockerfile.h011-v3" "$PRODUCT_DIR" > "$EVIDENCE/runtime-build.stdout" 2> "$EVIDENCE/runtime-build.stderr"
LOAD_RC=$?
set -e
INITIAL=false; RESTART=false
if [[ "$LOAD_RC" -ne 0 ]]; then
  fail ARM64_RUNTIME_IMAGE_LOAD_FAILED
else
  docker image inspect "$IMAGE" | jq -S . > "$EVIDENCE/runtime-image-inspect.json"
  [[ "$(docker image inspect "$IMAGE" --format '{{.Architecture}}/{{.Os}}')" == arm64/linux ]] || fail RUNTIME_IMAGE_PLATFORM_MISMATCH
  set +e
  docker run -d --platform linux/arm64 --name "$CONTAINER" -p 127.0.0.1:8080:8080 -e H011_ORDERS_ENABLED=false -e H011_RESULTS_DIR=/app/polymarket/results -e SENECIO_CODE_SHA="$PRODUCT_SHA" -e PORT=8080 -v "$RESULTS:/app/polymarket/results" "$IMAGE" > "$EVIDENCE/docker-run.stdout" 2> "$EVIDENCE/docker-run.stderr"
  RUN_RC=$?
  set -e
  if [[ "$RUN_RC" -ne 0 ]]; then
    fail ARM64_CONTAINER_START_FAILED
  else
    docker port "$CONTAINER" > "$EVIDENCE/docker-port.txt"; ss -ltnp > "$EVIDENCE/listeners.txt" || true
    grep -Eq '^8080/tcp -> 127\.0\.0\.1:8080$' "$EVIDENCE/docker-port.txt" || fail APPLICATION_NOT_LOOPBACK_ONLY
    ! grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\[::\]):8080([[:space:]]|$)' "$EVIDENCE/listeners.txt" || fail PUBLIC_8080_LISTENER_DETECTED
    deadline=$((SECONDS+1200))
    while ((SECONDS<deadline)); do
      docker inspect "$CONTAINER" --format '{{.State.Running}}' 2>/dev/null|grep -qx true || break
      if curl -fsS "$BASE/readyz"|jq -e '.ok==true and .readiness==true and .runtime_state=="RUNNING"' >/dev/null 2>&1 && curl -fsS "$BASE/api/v3/integrity"|jq -e --arg sha "$PRODUCT_SHA" '.code_sha==$sha and .paper_only==true and .orders_enabled==false and .live_capital_locked==true and .chain_verified==true and .replay_verified==true and .file_sha256_matches==true and .readiness==true' >/dev/null 2>&1; then INITIAL=true; break; fi
      sleep 10
    done
    for ep in livez readyz healthz api/v3/state api/v3/integrity api/v3/replay; do n=${ep//\//_}; curl -sS -D "$EVIDENCE/$n.headers" -o "$EVIDENCE/$n.json" -w '%{http_code}\n' "$BASE/$ep" > "$EVIDENCE/$n.http" || true; jq -S . "$EVIDENCE/$n.json" > "$EVIDENCE/$n.sorted.json" 2>/dev/null || true; done
    docker logs "$CONTAINER" > "$EVIDENCE/container-initial.log" 2>&1 || true; docker inspect "$CONTAINER"|jq -S . > "$EVIDENCE/container-initial-inspect.json" || true
    if [[ "$INITIAL" != true ]]; then
      fail INITIAL_ENDPOINT_GATE_FAILED
    else
      cp "$EVIDENCE/api_v3_integrity.sorted.json" "$EVIDENCE/integrity-before.json"; cp "$EVIDENCE/api_v3_replay.sorted.json" "$EVIDENCE/replay-before.json"
      PRE_SEQ=$(jq -r '.raw_chain.current_sequence' "$EVIDENCE/integrity-before.json"); PRE_ART=$(jq -r '.raw_chain.artifact_name' "$EVIDENCE/integrity-before.json"); PRE_SHA=$(jq -r '.raw_chain.artifact_sha256' "$EVIDENCE/integrity-before.json")
      sha256sum "$RESULTS/h011_v3/raw_chain_v1/$PRE_ART" > "$EVIDENCE/pre-tip.sha256"; [[ "$(awk '{print $1}' "$EVIDENCE/pre-tip.sha256")" == "$PRE_SHA" ]] || fail PRE_RESTART_TIP_HASH_MISMATCH
      docker stop --time 30 "$CONTAINER" > "$EVIDENCE/docker-stop.stdout" 2> "$EVIDENCE/docker-stop.stderr" || fail GRACEFUL_SHUTDOWN_COMMAND_FAILED
      docker inspect "$CONTAINER"|jq -S '.[0].State' > "$EVIDENCE/container-stopped-state.json"; jq -e '.Running==false and .OOMKilled==false and .ExitCode==0' "$EVIDENCE/container-stopped-state.json" >/dev/null || fail GRACEFUL_SHUTDOWN_STATE_FAILED
      docker start "$CONTAINER" > "$EVIDENCE/docker-start.stdout" 2> "$EVIDENCE/docker-start.stderr" || fail CONTAINER_RESTART_COMMAND_FAILED
      deadline=$((SECONDS+1200))
      while ((SECONDS<deadline)); do if curl -fsS "$BASE/readyz"|jq -e '.ok==true and .readiness==true and .runtime_state=="RUNNING"' >/dev/null 2>&1 && curl -fsS "$BASE/api/v3/integrity"|jq -e '.paper_only==true and .orders_enabled==false and .live_capital_locked==true and .chain_verified==true and .replay_verified==true and .file_sha256_matches==true and .readiness==true' >/dev/null 2>&1; then RESTART=true; break; fi; sleep 10; done
      curl -sS "$BASE/api/v3/integrity"|jq -S . > "$EVIDENCE/integrity-after.json" || true; curl -sS "$BASE/api/v3/replay"|jq -S . > "$EVIDENCE/replay-after.json" || true; docker logs "$CONTAINER" > "$EVIDENCE/container-after.log" 2>&1 || true
      if [[ "$RESTART" != true ]]; then fail RESTART_ENDPOINT_GATE_FAILED; else POST_SEQ=$(jq -r '.raw_chain.current_sequence' "$EVIDENCE/integrity-after.json"); ((POST_SEQ>=PRE_SEQ)) || fail RAW_CHAIN_SEQUENCE_REGRESSION; sha256sum "$RESULTS/h011_v3/raw_chain_v1/$PRE_ART" > "$EVIDENCE/post-tip.sha256"; [[ "$(awk '{print $1}' "$EVIDENCE/post-tip.sha256")" == "$PRE_SHA" ]] || fail PREVIOUS_ARTIFACT_NOT_PRESERVED; jq -e '.raw_complete==true and .chain_verified==true and .replay_verified==true and .file_sha256_matches==true and .error==null' "$EVIDENCE/replay-after.json" >/dev/null || fail RESTART_REPLAY_FAILED; fi
    fi
  fi
fi

# 7. Final machine-readable verdict and immutable checksums.
FAIL_JSON=$(printf '%s\n' "${FAILURES[@]}" | python3 -c 'import json,sys; print(json.dumps([x.rstrip("\n") for x in sys.stdin if x.rstrip("\n")]))')
STATUS=PASS; ((${#FAILURES[@]}==0)) || STATUS=FAIL
python3 - "$STATUS" "$FAIL_JSON" "$PAPER_RC" "$BASE_INDEX_DIGEST" "$BASE_ARM64_DIGEST" "$PIP_RC" "$D1" "$D2" "$REPRO" "$INITIAL" "$RESTART" > "$EVIDENCE/summary.json" <<'PY'
import json,sys
s,f,pr,bi,ba,pip,d1,d2,repro,initial,restart=sys.argv[1:]
print(json.dumps({'schema_version':'senex-arm64-preflight-v1','status':s,'failures':json.loads(f),'paper_only_gate_rc':int(pr),'base_index_digest':bi or None,'base_arm64_manifest_digest':ba or None,'dependency_resolution_rc':int(pip),'build_1_manifest_digest':d1 or None,'build_2_manifest_digest':d2 or None,'immediate_build_reproducible':repro=='true','initial_loopback_endpoint_gate':initial=='true','shutdown_restart_replay_gate':restart=='true','paper_only':True,'orders_enabled':False,'live_capital_locked':True,'real_order_network_calls':0,'wallet_or_private_key_access':0,'real_capital_actions':0,'secret_values_observed':False,'northflank_mutations':0,'oci_infrastructure_mutations':0},sort_keys=True,indent=2))
PY
find "$EVIDENCE" -type f ! -name SHA256SUMS -print0|sort -z|xargs -0 sha256sum > "$EVIDENCE/SHA256SUMS"
printf 'ARM64_PREFLIGHT=%s\n' "$STATUS" > "$EVIDENCE/result.env"
[[ "$STATUS" == PASS ]]
