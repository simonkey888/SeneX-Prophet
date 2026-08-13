"""Runtime learning bridge for SENEX's SingleDecisionCore.

The production predictor is extended with one bounded, deterministic feedback
seam sourced only from proof-qualified, symbol-scoped historical evidence.
AUD-059 adds explicit pre-decision provenance so every new PAPER prediction can
prove exactly which already-settled rows and effective weights were available
before the decision was made.

Safety invariants:
- learning consumes only rows that pass ``is_proof_qualified``;
- primary evidence remains the 1h settlement window;
- weights do not mutate before 10 qualified examples for the symbol;
- at most the latest 50 qualified examples are replayed;
- each weight is clamped to +/-25% of its code-defined base value;
- failures fail open to the base PAPER predictor, never to live execution;
- no wallet, signer, order placement, or live-capital behavior is added here.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent
_ORACLE_DIR = _ROOT_DIR / "oracle"
_ORIGINAL_PATH = _ORACLE_DIR / "institutional_core.py"
if str(_ORACLE_DIR) not in sys.path:
    sys.path.insert(0, str(_ORACLE_DIR))

_spec = importlib.util.spec_from_file_location(
    "_senex_original_institutional_core", _ORIGINAL_PATH
)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load original institutional_core from {_ORIGINAL_PATH}")
_original = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_original)

for _name in dir(_original):
    if _name == "SingleDecisionCore" or _name.startswith("__"):
        continue
    globals()[_name] = getattr(_original, _name)

OriginalSingleDecisionCore = _original.SingleDecisionCore

LEARNING_VERSION = "proof-qualified-replay-v2-aud059"
MIN_LEARNING_EXAMPLES = 10
MAX_LEARNING_EXAMPLES = 50
FETCH_LIMIT = 120
MAX_RELATIVE_DRIFT = 0.25
FETCH_CACHE_TTL_S = 60.0

_PRESSURE_TO_WEIGHT = {
    "orderflow": "orderflow",
    "volume_delta": "volume_delta",
    "bidask": "bidask_imbalance",
    "funding": "funding_signal",
    "oi": "oi_momentum",
    "price_momentum": "price_momentum",
}
_fetch_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _normalize_symbol(symbol: str) -> str:
    return (symbol or "").replace("/", "").upper().strip()


def _proof_gate(row: dict[str, Any]) -> bool:
    try:
        from backend.settlement_proof import is_proof_qualified
    except ImportError:
        from senecio_polymarket.backend.settlement_proof import is_proof_qualified
    return bool(is_proof_qualified(row))


def _supabase_headers(key: str) -> dict[str, str]:
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ") and key.count(".") == 2:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _weights_payload(weights: dict[str, Any]) -> dict[str, float]:
    return {k: round(float(v), 6) for k, v in sorted(weights.items())}


def effective_weights_hash(weights: dict[str, Any]) -> str:
    """Deterministic hash of decision-time effective weights."""
    payload = json.dumps(
        _weights_payload(weights), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fetch_authoritative_rows(symbol: str) -> list[dict[str, Any]]:
    """Fetch recent settled candidates for one symbol from canonical storage.

    The response is still only a candidate set. Strict proof qualification is
    applied locally before any row can influence a weight.
    """
    normalized = _normalize_symbol(symbol)
    if not normalized:
        return []
    now = time.monotonic()
    cached = _fetch_cache.get(normalized)
    if cached and now - cached[0] <= FETCH_CACHE_TTL_S:
        return list(cached[1])

    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY") or ""
    table = os.environ.get("SUPABASE_TABLE", "oracle_predictions")
    if not url or not key:
        return []
    params = {
        "select": "id,ts,symbol,prediction,confidence,price_now,outcome,audit",
        "symbol": f"eq.{normalized}",
        "outcome": "in.(WIN,LOSS)",
        "order": "ts.desc",
        "limit": str(FETCH_LIMIT),
    }
    with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        response = client.get(
            f"{url}/rest/v1/{table}", headers=_supabase_headers(key), params=params
        )
    if response.status_code != 200:
        raise RuntimeError(f"supabase_learning_http_{response.status_code}")
    data = response.json()
    rows = data if isinstance(data, list) else []
    _fetch_cache[normalized] = (now, list(rows))
    return rows


def _directional_return(row: dict[str, Any]) -> float | None:
    audit = row.get("audit") or {}
    if not isinstance(audit, dict):
        return None
    dual = audit.get("outcomes_dual") or {}
    if not isinstance(dual, dict):
        return None
    try:
        origin = float(row.get("price_now"))
        later = float(dual.get("price_1h_later"))
    except (TypeError, ValueError):
        return None
    if origin <= 0 or later <= 0:
        return None
    raw = (later - origin) / origin
    direction = (row.get("prediction") or "").upper()
    if direction == "LONG":
        return raw
    if direction == "SHORT":
        return -raw
    return None


def _reset_replay_state(core: Any) -> dict[str, float]:
    base = dict(getattr(core, "_senex_base_weights", core.weights))
    core.weights.clear()
    core.weights.update(base)
    calibration = getattr(core, "_calibration_window", None)
    if calibration is not None:
        calibration.clear()
    by_direction = getattr(core, "_calibration_by_direction", {})
    if isinstance(by_direction, dict):
        for window in by_direction.values():
            try:
                window.clear()
            except Exception:
                pass
    mutation_log = getattr(core, "_mutation_log", None)
    if mutation_log is not None:
        mutation_log.clear()
    if hasattr(core, "_long_loss_streak_under_bear"):
        core._long_loss_streak_under_bear = 0
    return base


def replay_authoritative_learning(
    core: Any,
    rows: list[dict[str, Any]],
    symbol: str,
) -> dict[str, Any]:
    """Replay only already-settled proof-qualified evidence into ``core``.

    ``rows`` is a pre-decision snapshot. The returned provenance records the
    number of qualified examples available at that moment, the exact selected
    IDs (latest 50 maximum), and a deterministic effective-weight hash. No
    outcome from the prediction being produced can be present in this snapshot.
    """
    normalized = _normalize_symbol(symbol)
    base_weights = _reset_replay_state(core)
    available = [
        row
        for row in rows
        if _normalize_symbol(str(row.get("symbol") or "")) == normalized
        and _proof_gate(row)
    ]
    available.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("id") or "")))
    proof_qualified_available_before_decision = len(available)
    qualified = available[-MAX_LEARNING_EXAMPLES:]

    wins = sum(1 for row in qualified if row.get("outcome") == "WIN")
    losses = sum(1 for row in qualified if row.get("outcome") == "LOSS")
    for row in qualified:
        correct = row.get("outcome") == "WIN"
        try:
            core.record_outcome(correct)
        except Exception:
            pass
        audit = row.get("audit") or {}
        pipeline = audit.get("pipeline") if isinstance(audit, dict) else {}
        step2 = pipeline.get("step2_features") if isinstance(pipeline, dict) else {}
        if not isinstance(step2, dict):
            step2 = {}
        try:
            core.record_outcome_directional(
                str(row.get("prediction") or ""),
                correct,
                float(step2.get("conviction") or row.get("confidence") or 0.0),
                str(step2.get("regime_4h") or "NEUTRAL"),
            )
        except Exception:
            pass

    state: dict[str, Any] = {
        "version": LEARNING_VERSION,
        "learning_version": LEARNING_VERSION,
        "symbol": normalized,
        "status": "WARMUP",
        "evidence_cut": "PRE_DECISION_SNAPSHOT",
        "uses_only_prior_settled_evidence": True,
        "proof_qualified_available_before_decision": proof_qualified_available_before_decision,
        "min_examples": MIN_LEARNING_EXAMPLES,
        "max_replayed_examples": MAX_LEARNING_EXAMPLES,
        "proof_qualified_n": len(qualified),
        "wins": wins,
        "losses": losses,
        "source_prediction_ids": [row.get("id") for row in qualified],
        "max_relative_drift": MAX_RELATIVE_DRIFT,
        "base_weights": _weights_payload(base_weights),
        "effective_weights": _weights_payload(core.weights),
        "effective_weights_hash": effective_weights_hash(core.weights),
        "mutations": 0,
    }
    if len(qualified) < MIN_LEARNING_EXAMPLES:
        return state

    learning_rate = float(getattr(core, "learning_rate", 0.03))
    weight_min = float(getattr(core, "weight_min", 0.05))
    weight_max = float(getattr(core, "weight_max", 3.0))
    mutations = 0
    for row in qualified:
        audit = row.get("audit") or {}
        pipeline = audit.get("pipeline") if isinstance(audit, dict) else {}
        features = pipeline.get("step2_features") if isinstance(pipeline, dict) else {}
        if not isinstance(features, dict):
            continue
        pressures = features.get("pressures") or {}
        if not isinstance(pressures, dict):
            continue
        pnl_pct = _directional_return(row)
        if pnl_pct is None:
            continue
        pnl_pct = max(-0.02, min(0.02, pnl_pct))
        conviction = float(features.get("conviction") or row.get("confidence") or 0.0)
        expected = conviction * 0.01
        scaled_signal = math.tanh((pnl_pct - expected) * 100.0)
        correct = row.get("outcome") == "WIN"
        side = (row.get("prediction") or "").upper()
        for pressure_name, pressure_value_raw in pressures.items():
            weight_name = _PRESSURE_TO_WEIGHT.get(pressure_name)
            if not weight_name or weight_name not in core.weights:
                continue
            try:
                pressure_value = float(pressure_value_raw)
            except (TypeError, ValueError):
                continue
            if abs(pressure_value) <= 1e-12:
                continue
            pressure_agreed = (
                (side == "LONG" and pressure_value > 0)
                or (side == "SHORT" and pressure_value < 0)
            )
            if correct:
                delta = (
                    learning_rate * abs(scaled_signal) * 0.5
                    if pressure_agreed else -learning_rate * 0.1
                )
            else:
                delta = (
                    -learning_rate * abs(scaled_signal)
                    if pressure_agreed else learning_rate * abs(scaled_signal) * 0.3
                )
            base = float(base_weights[weight_name])
            low = max(weight_min, base * (1.0 - MAX_RELATIVE_DRIFT))
            high = min(weight_max, base * (1.0 + MAX_RELATIVE_DRIFT))
            old = float(core.weights[weight_name])
            new = max(low, min(high, old + delta))
            if abs(new - old) > 1e-12:
                core.weights[weight_name] = new
                mutations += 1

    state["status"] = "ACTIVE"
    state["mutations"] = mutations
    state["effective_weights"] = _weights_payload(core.weights)
    state["effective_weights_hash"] = effective_weights_hash(core.weights)
    return state


class SingleDecisionCore(OriginalSingleDecisionCore):
    """Original SDC plus authoritative, replayable PAPER feedback."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._senex_base_weights = dict(self.weights)
        self._authoritative_learning_symbol: str | None = None
        self._authoritative_learning_state: dict[str, Any] = {
            "version": LEARNING_VERSION,
            "learning_version": LEARNING_VERSION,
            "status": "NOT_LOADED",
            "proof_qualified_n": 0,
            "proof_qualified_available_before_decision": 0,
            "evidence_cut": "PRE_DECISION_SNAPSHOT",
            "uses_only_prior_settled_evidence": True,
            "effective_weights_hash": effective_weights_hash(self.weights),
        }

    def _load_learning_for_symbol(self, symbol: str) -> None:
        normalized = _normalize_symbol(symbol)
        if self._authoritative_learning_symbol == normalized:
            return
        self._authoritative_learning_symbol = normalized
        if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_KEY"):
            self._authoritative_learning_state = {
                "version": LEARNING_VERSION,
                "learning_version": LEARNING_VERSION,
                "symbol": normalized,
                "status": "DISABLED_NO_CONFIG",
                "evidence_cut": "PRE_DECISION_SNAPSHOT",
                "uses_only_prior_settled_evidence": True,
                "proof_qualified_n": 0,
                "proof_qualified_available_before_decision": 0,
                "source_prediction_ids": [],
                "effective_weights": _weights_payload(self.weights),
                "effective_weights_hash": effective_weights_hash(self.weights),
            }
            return
        try:
            candidates = fetch_authoritative_rows(normalized)
            self._authoritative_learning_state = replay_authoritative_learning(
                self, candidates, normalized
            )
        except Exception as exc:
            self.weights.clear()
            self.weights.update(self._senex_base_weights)
            self._authoritative_learning_state = {
                "version": LEARNING_VERSION,
                "learning_version": LEARNING_VERSION,
                "symbol": normalized,
                "status": "UNAVAILABLE",
                "error": type(exc).__name__,
                "evidence_cut": "PRE_DECISION_SNAPSHOT",
                "uses_only_prior_settled_evidence": True,
                "proof_qualified_n": 0,
                "proof_qualified_available_before_decision": 0,
                "source_prediction_ids": [],
                "effective_weights": _weights_payload(self.weights),
                "effective_weights_hash": effective_weights_hash(self.weights),
            }

    def decide(self, market: dict, risk_state: dict, execution_state: dict) -> dict:
        # Learning snapshot is loaded before the prediction decision.
        self._load_learning_for_symbol(str(market.get("symbol") or ""))
        action_vector = super().decide(market, risk_state, execution_state)
        pipeline = action_vector.setdefault("pipeline", {})
        step2 = pipeline.setdefault("step2_features", {})
        if isinstance(step2, dict):
            step2["learning_state_v1"] = dict(self._authoritative_learning_state)
        return action_vector
