from __future__ import annotations

import pytest

from polymarket.h011_v3_fs_probe import run_probe


def test_isolated_filesystem_probe_cleans_owned_directory(tmp_path):
    result = run_probe(tmp_path)
    assert result["checks"]["owned_cleanup"]["pass"] is True
    assert not __import__("pathlib").Path(result["probe_directory"]).exists()
    if not result["storage_compatible"]:
        pytest.skip(f"filesystem capability absent: {result['checks'].get('probe_error')}")
    required = {
        "flock", "hardlink", "rename_exchange", "file_fsync",
        "directory_fsync", "o_nofollow", "chmod_0444",
        "inode_stability", "process_restart_persistence", "same_filesystem",
    }
    assert required.issubset(result["checks"])
    assert all(result["checks"][name]["pass"] for name in required)
