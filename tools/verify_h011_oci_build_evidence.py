#!/usr/bin/env python3
"""Derive per-build base and runtime-lock evidence from an OCI image layout."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
import tempfile
from pathlib import Path
from typing import Any

RUNTIME_LOCK_PATH = "app/polymarket/requirements-h011-v3-runtime.txt"
SUPPORTED_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _blob_path(layout: Path, digest: str) -> Path:
    algorithm, separator, value = digest.partition(":")
    if separator != ":" or algorithm != "sha256" or len(value) != 64:
        raise ValueError(f"unsupported OCI digest: {digest}")
    path = layout / "blobs" / algorithm / value
    if not path.is_file():
        raise FileNotFoundError(f"OCI blob missing: {digest}")
    actual = _digest_bytes(path.read_bytes())
    if actual != digest:
        raise ValueError(f"OCI blob digest mismatch: expected={digest} actual={actual}")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _normalized_tar_name(name: str) -> str:
    normalized = name.lstrip("./")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _runtime_lock_from_layers(layout: Path, layers: list[dict[str, Any]]) -> bytes:
    content: bytes | None = None
    whiteout = "app/polymarket/.wh.requirements-h011-v3-runtime.txt"
    for layer in layers:
        media_type = str(layer.get("mediaType", ""))
        if media_type not in SUPPORTED_LAYER_MEDIA_TYPES:
            raise ValueError(f"unsupported OCI layer media type: {media_type}")
        digest = str(layer.get("digest", ""))
        blob = _blob_path(layout, digest)
        with tarfile.open(blob, mode="r:*") as archive:
            for member in archive.getmembers():
                name = _normalized_tar_name(member.name)
                if name == whiteout:
                    content = None
                elif name == RUNTIME_LOCK_PATH:
                    if not member.isfile():
                        raise ValueError("runtime lock path is not a regular file")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("runtime lock file could not be read")
                    content = stream.read()
    if content is None:
        raise FileNotFoundError(
            f"runtime lock absent from final OCI rootfs: /{RUNTIME_LOCK_PATH}"
        )
    return content


def derive(
    *,
    layout: Path,
    base_manifest_path: Path,
    expected_base_digest: str,
    runtime_lock_source: Path,
    output_dir: Path,
    prefix: str,
) -> dict[str, Any]:
    index = _load_json(layout / "index.json")
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ValueError("OCI layout must contain exactly one image manifest")
    image_manifest_digest = str(manifests[0].get("digest", ""))
    image_manifest = _load_json(_blob_path(layout, image_manifest_digest))
    image_layers = image_manifest.get("layers")
    if not isinstance(image_layers, list) or not image_layers:
        raise ValueError("image manifest has no layers")

    base_raw = base_manifest_path.read_bytes()
    observed_base_digest = _digest_bytes(base_raw)
    if observed_base_digest != expected_base_digest:
        raise ValueError(
            "base child manifest digest mismatch: "
            f"expected={expected_base_digest} observed={observed_base_digest}"
        )
    base_manifest = json.loads(base_raw)
    if not isinstance(base_manifest, dict):
        raise ValueError("base child manifest must be a JSON object")
    base_layers = base_manifest.get("layers")
    if not isinstance(base_layers, list) or not base_layers:
        raise ValueError("base child manifest has no layers")
    base_digests = [str(layer.get("digest", "")) for layer in base_layers]
    image_digests = [str(layer.get("digest", "")) for layer in image_layers]
    if image_digests[: len(base_digests)] != base_digests:
        raise ValueError("built image does not contain the expected base layer prefix")

    source_lock = runtime_lock_source.read_bytes()
    image_lock = _runtime_lock_from_layers(layout, image_layers)
    if image_lock != source_lock:
        raise ValueError("runtime lock embedded in image differs from source lock")
    runtime_lock_sha = hashlib.sha256(image_lock).hexdigest()

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{prefix}-base-digest.txt").write_text(
        observed_base_digest + "\n", encoding="utf-8"
    )
    (output_dir / f"{prefix}-base-layer-digests.txt").write_text(
        "\n".join(base_digests) + "\n", encoding="utf-8"
    )
    normalized_lock_line = (
        f"{runtime_lock_sha}  /app/polymarket/requirements-h011-v3-runtime.txt\n"
    )
    (output_dir / f"{prefix}-runtime-lock-sha256.txt").write_text(
        normalized_lock_line, encoding="utf-8"
    )
    report = {
        "schema_version": "senex-h011-oci-build-evidence-v1",
        "prefix": prefix,
        "image_manifest_digest": image_manifest_digest,
        "base_digest": observed_base_digest,
        "base_layer_count": len(base_digests),
        "base_layer_prefix_verified": True,
        "runtime_lock_path": "/" + RUNTIME_LOCK_PATH,
        "runtime_lock_sha256": runtime_lock_sha,
        "runtime_lock_matches_source": True,
    }
    (output_dir / f"{prefix}-derived-build-evidence.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return report


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="senex-oci-evidence-") as temp:
        root = Path(temp)
        layout = root / "oci"
        blobs = layout / "blobs" / "sha256"
        blobs.mkdir(parents=True)
        lock = b"example==1.0 --hash=sha256:" + b"a" * 64 + b"\n"
        source = root / "runtime.txt"
        source.write_bytes(lock)
        base_layer = _tar_bytes({"usr/lib/base.txt": b"base\n"})
        app_layer = _tar_bytes({RUNTIME_LOCK_PATH: lock})
        base_layer_digest = _digest_bytes(base_layer)
        app_layer_digest = _digest_bytes(app_layer)
        for digest, payload in (
            (base_layer_digest, base_layer),
            (app_layer_digest, app_layer),
        ):
            (blobs / digest.split(":", 1)[1]).write_bytes(payload)
        base_manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": "sha256:" + "0" * 64,
                "size": 0,
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": base_layer_digest,
                    "size": len(base_layer),
                }
            ],
        }
        base_raw = json.dumps(base_manifest, separators=(",", ":")).encode()
        base_path = root / "base.json"
        base_path.write_bytes(base_raw)
        image_manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": "sha256:" + "0" * 64,
                "size": 0,
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": base_layer_digest,
                    "size": len(base_layer),
                },
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": app_layer_digest,
                    "size": len(app_layer),
                },
            ],
        }
        image_raw = json.dumps(image_manifest, separators=(",", ":")).encode()
        image_digest = _digest_bytes(image_raw)
        (blobs / image_digest.split(":", 1)[1]).write_bytes(image_raw)
        (layout / "index.json").write_text(
            json.dumps({"schemaVersion": 2, "manifests": [{"digest": image_digest}]}),
            encoding="utf-8",
        )
        output = root / "evidence"
        report = derive(
            layout=layout,
            base_manifest_path=base_path,
            expected_base_digest=_digest_bytes(base_raw),
            runtime_lock_source=source,
            output_dir=output,
            prefix="build-1",
        )
        assert report["base_layer_prefix_verified"] is True
        assert report["runtime_lock_matches_source"] is True
        assert (output / "build-1-runtime-lock-sha256.txt").is_file()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--oci-dir", type=Path)
    parser.add_argument("--base-manifest", type=Path)
    parser.add_argument("--expected-base-digest")
    parser.add_argument("--runtime-lock-source", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--prefix")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print(json.dumps({"status": "PASS", "self_test": True}, sort_keys=True))
        return 0
    required = {
        "oci_dir": args.oci_dir,
        "base_manifest": args.base_manifest,
        "expected_base_digest": args.expected_base_digest,
        "runtime_lock_source": args.runtime_lock_source,
        "output_dir": args.output_dir,
        "prefix": args.prefix,
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        parser.error("missing required arguments: " + ", ".join(missing))
    report = derive(
        layout=args.oci_dir,
        base_manifest_path=args.base_manifest,
        expected_base_digest=str(args.expected_base_digest),
        runtime_lock_source=args.runtime_lock_source,
        output_dir=args.output_dir,
        prefix=str(args.prefix),
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
