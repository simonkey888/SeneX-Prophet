from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/h011-arm64-reproducibility.yml"
HARNESS = ROOT / "tools/verify_h011_arm64_reproducibility.sh"
QEMU_DIGEST = "sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
BUILDKIT_DIGEST = "sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec"


def test_workflow_pins_qemu_buildkit_and_always_verifies_checksums() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert f"image: docker.io/tonistiigi/binfmt@{QEMU_DIGEST}" in text
    assert f"image=docker.io/moby/buildkit@{BUILDKIT_DIGEST}" in text
    assert "cache-image: false" in text
    assert (
        "- name: Verify portable evidence checksums\n"
        "        if: always()"
    ) in text
    assert "- name: Upload checksummed evidence\n        if: always()" in text
    assert "- name: Enforce exact-head verdict\n        if: always()" in text


def test_harness_uses_candidate_identity_and_pinned_internal_builders() -> None:
    text = HARNESS.read_text(encoding="utf-8")
    assert 'CANDIDATE_HEAD_SHA="$HEAD_SHA"' in text
    assert 'CANDIDATE_HEAD_TREE="$HEAD_TREE"' in text
    assert 'EVENT_SHA="${GITHUB_SHA:-unknown}"' in text
    assert 'IMAGE="senex-h011-repro:${CANDIDATE_HEAD_SHA}"' in text
    assert 'IMAGE="senex-h011-repro:${GITHUB_SHA:-local}"' not in text
    assert f'QEMU_BINFMT_DIGEST="{QEMU_DIGEST}"' in text
    assert f'BUILDKIT_DIGEST="{BUILDKIT_DIGEST}"' in text
    assert '--driver-opt "image=$BUILDKIT_REFERENCE"' in text
    assert "moby/buildkit:buildx-stable-1" not in text
    assert '$1=="BuildKit" && $2=="version:" {print $3; exit}' in text
    assert '$1=="BuildKit:" {print $2; exit}' not in text


def test_harness_proves_pr_event_identity_and_failure_checksums() -> None:
    text = HARNESS.read_text(encoding="utf-8")
    for marker in (
        "--identity-probe",
        "IDENTITY_SELF_TEST_EVENT_SHA_INVALID",
        "IDENTITY_SELF_TEST_IMAGE_TAG_INVALID",
        "IDENTITY_SELF_TEST_REVISION_LABEL_INVALID",
        "CONTROLLED_FAILURE_CHECKSUMS_INVALID",
        "runtime-image-identity.env",
        "runtime-image-observed-tag.txt",
        "IMAGE_OBSERVED_TAG",
        "RUNTIME_IMAGE_IDENTITY_FIELD_EMPTY_",
        "CANDIDATE_HEAD_SHA",
        "CANDIDATE_HEAD_TREE",
        "EVENT_SHA",
        "IMAGE_TAG",
        "IMAGE_ID",
        "IMAGE_REVISION_LABEL",
        "QEMU_BINFMT_REFERENCE",
        "QEMU_BINFMT_DIGEST",
        "BUILDKIT_REFERENCE",
        "BUILDKIT_DIGEST",
        "BUILDKIT_VERSION",
    ):
        assert marker in text

def test_runtime_tag_and_identity_completeness_are_observed_not_tautological() -> None:
    text = HARNESS.read_text(encoding="utf-8")
    assert "{{range .RepoTags}}{{println .}}{{end}}" in text
    assert '[[ "$IMAGE_OBSERVED_TAG" == "$IMAGE" ]]' in text
    assert '[[ "$IMAGE" == "senex-h011-repro:${CANDIDATE_HEAD_SHA}" ]]' not in text
    assert 'wc -l < "$EVIDENCE/runtime-image-identity.env"' not in text
    assert "for identity_field in" in text
    assert '[[ -n "${!identity_field}"' in text
