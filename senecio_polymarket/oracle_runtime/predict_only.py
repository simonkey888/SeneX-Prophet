"""Runtime bridge for the production ``predict_only`` module.

The Docker image preserves the original predictor as ``predict_only_base.py``
and installs this module at ``/app/oracle/predict_only.py``.  Every original
function is re-exported unchanged except ``run_prediction``.  During that one
call, the local import name ``institutional_core`` is deterministically bound
to the proof-qualified learning wrapper.

This avoids relying on ``sys.path`` ordering: the legacy predictor inserts its
own directory at index 0, so PYTHONPATH precedence alone is not sufficient.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from oracle_runtime import institutional_core as _learning_core

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent
_ORACLE_DIR = _ROOT_DIR / "oracle"
_BASE_PATH = _ORACLE_DIR / "predict_only_base.py"
if not _BASE_PATH.exists():
    # Source-tree / CI path. In the Docker image the original is renamed to
    # predict_only_base.py before this bridge is copied into /app/oracle.
    _BASE_PATH = _ORACLE_DIR / "predict_only.py"

_spec = importlib.util.spec_from_file_location("_senex_predict_only_base", _BASE_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load original predict_only from {_BASE_PATH}")
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

for _name in dir(_base):
    if _name == "run_prediction" or _name.startswith("__"):
        continue
    globals()[_name] = getattr(_base, _name)


def run_prediction(market_data: dict) -> dict:
    """Run the unchanged predictor with the authoritative learning SDC bound."""
    previous = sys.modules.get("institutional_core")
    sys.modules["institutional_core"] = _learning_core
    try:
        return _base.run_prediction(market_data)
    finally:
        if previous is None:
            sys.modules.pop("institutional_core", None)
        else:
            sys.modules["institutional_core"] = previous
