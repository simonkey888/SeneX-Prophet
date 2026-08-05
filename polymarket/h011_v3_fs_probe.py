"""Isolated filesystem capability probe for the SENEX raw-chain contract.

This probe is never executed automatically. It operates only inside a new,
randomly named child directory and removes only that owned directory.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import h011_v3_raw_transaction as rt
except ModuleNotFoundError:
    from polymarket import h011_v3_raw_transaction as rt  # type: ignore


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_probe(parent: Path) -> dict[str, Any]:
    parent = parent.absolute()
    parent_stat = os.lstat(parent)
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise RuntimeError(f"probe parent must be a real directory: {parent}")
    probe = parent / f"senex_fs_probe_{uuid.uuid4()}"
    if probe.exists():
        raise RuntimeError(f"probe directory unexpectedly exists: {probe}")

    checks: dict[str, dict[str, Any]] = {}
    created = False
    try:
        probe.mkdir(mode=0o700)
        created = True
        _fsync_dir(parent)
        root_fd = os.open(probe, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            checks["same_filesystem"] = {"pass": os.fstat(root_fd).st_dev == parent_stat.st_dev}

            lock_fd = os.open("probe.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                checks["flock"] = {"pass": True}
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

            file_fd = os.open("source", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
            try:
                os.write(file_fd, b"senex-fs-probe-v1")
                os.fsync(file_fd)
                checks["file_fsync"] = {"pass": True}
                before = os.fstat(file_fd)
                os.fchmod(file_fd, 0o444)
                os.fsync(file_fd)
            finally:
                os.close(file_fd)

            source_fd = os.open("source", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                reopened = os.fstat(source_fd)
                checks["o_nofollow"] = {"pass": True}
                checks["chmod_0444"] = {"pass": stat.S_IMODE(reopened.st_mode) == 0o444}
                checks["inode_stability"] = {
                    "pass": (before.st_dev, before.st_ino) == (reopened.st_dev, reopened.st_ino)
                }
            finally:
                os.close(source_fd)

            os.link("source", "hardlink", src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
            hard_stat = os.stat("hardlink", dir_fd=root_fd, follow_symlinks=False)
            checks["hardlink"] = {
                "pass": (hard_stat.st_dev, hard_stat.st_ino) == (before.st_dev, before.st_ino)
            }
            os.fsync(root_fd)
            checks["directory_fsync"] = {"pass": True}

            for name, payload in (("exchange_a", b"A"), ("exchange_b", b"B")):
                fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            rt._renameat2_exchange(root_fd, "exchange_a", "exchange_b")  # noqa: SLF001
            os.fsync(root_fd)
            a = (probe / "exchange_a").read_bytes()
            b = (probe / "exchange_b").read_bytes()
            checks["rename_exchange"] = {"pass": a == b"B" and b == b"A"}

            token = uuid.uuid4().hex
            (probe / "restart_token").write_text(token, encoding="ascii")
            token_fd = os.open("restart_token", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                os.fsync(token_fd)
            finally:
                os.close(token_fd)
            os.fsync(root_fd)
            code = (
                "from pathlib import Path; import sys; "
                "p=Path(sys.argv[1]); expected=sys.argv[2]; "
                "raise SystemExit(0 if p.read_text(encoding='ascii') == expected else 7)"
            )
            completed = subprocess.run(
                [sys.executable, "-c", code, str(probe / "restart_token"), token],
                check=False,
                capture_output=True,
                text=True,
            )
            checks["process_restart_persistence"] = {
                "pass": completed.returncode == 0,
                "returncode": completed.returncode,
                "stderr": completed.stderr[-500:],
            }
        finally:
            os.close(root_fd)
    except Exception as exc:
        checks.setdefault("probe_error", {"pass": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        cleanup_error = None
        if created:
            try:
                shutil.rmtree(probe)
                _fsync_dir(parent)
            except Exception as exc:
                cleanup_error = f"{type(exc).__name__}: {exc}"
        checks["owned_cleanup"] = {"pass": cleanup_error is None and not probe.exists(), "error": cleanup_error}

    passed = all(value.get("pass") is True for value in checks.values())
    return {
        "schema_version": "senex-fs-probe-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parent": str(parent),
        "probe_directory": str(probe),
        "checks": checks,
        "storage_compatible": passed,
        "production_probe": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_probe(args.parent)
    encoded = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if result["storage_compatible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
