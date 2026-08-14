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
from datetime import datetime, timezone
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

LEARNING_VERSION = "proof-qualified-replay-v4-aud061-r1"
MIN_LEARNING_EXAMPLES = 10
MAX_LEARNING_EXAMPLES = 50
FETCH_LIMIT = 240
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


def _canonical_json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _timestamp_epoch(value: Any) -> float | None:
    """Normalize ISO, datetime, seconds, or milliseconds for causal ordering."""
    try:
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        if isinstance(value, (int, float)):
            number = float(value)
            return number / 1000.0 if number > 10_000_000_000 else number
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return _timestamp_epoch(float(text))
        except ValueError:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _settlement_observed_epoch(row: dict[str, Any]) -> float | None:
    """Use explicit observation provenance, never inferred horizon expiry."""
    audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
    dual = audit.get("outcomes_dual") if isinstance(audit, dict) else {}
    if not isinstance(dual, dict):
        dual = {}
    provenance = dual.get("settlement_observation_v1") or {}
    values = (
        provenance.get("observed_at") if isinstance(provenance, dict) else None,
        dual.get("settled_at"), dual.get("verified_at"), dual.get("reconciled_at"),
        row.get("_senex_snapshot_observed_at_epoch"),
    )
    for value in values:
        parsed = _timestamp_epoch(value)
        if parsed is not None:
            return parsed
    return None


def _evidence_hash(rows: list[dict[str, Any]]) -> str:
    """Hash only the causal evidence fields consumed by the replay."""
    evidence = []
    for row in rows:
        audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        pipeline = audit.get("pipeline") if isinstance(audit, dict) else {}
        step2 = pipeline.get("step2_features") if isinstance(pipeline, dict) else {}
        dual = audit.get("outcomes_dual") if isinstance(audit, dict) else {}
        evidence.append({
            "id": row.get("id"), "ts": row.get("ts"), "symbol": row.get("symbol"),
            "prediction": row.get("prediction"), "confidence": row.get("confidence"),
            "price_now": row.get("price_now"), "outcome": row.get("outcome"),
            "price_1h_later": dual.get("price_1h_later") if isinstance(dual, dict) else None,
            "settlement_observed_epoch": _settlement_observed_epoch(row),
            "features": step2 if isinstance(step2, dict) else {},
        })
    return _canonical_json_hash(evidence)


def _code_hash() -> str:
    payload = _ORIGINAL_PATH.read_bytes() + Path(__file__).read_bytes()
    return hashlib.sha256(payload).hexdigest()


def _config_hash(base_weights: dict[str, Any]) -> str:
    return _canonical_json_hash({
        "version": LEARNING_VERSION,
        "min_examples": MIN_LEARNING_EXAMPLES,
        "max_examples": MAX_LEARNING_EXAMPLES,
        "max_relative_drift": MAX_RELATIVE_DRIFT,
        "fetch_limit": FETCH_LIMIT,
        "fetch_cache_ttl_s": FETCH_CACHE_TTL_S,
        "base_weights": _weights_payload(base_weights),
    })


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
    # The public GET snapshot proves that every returned settlement existed no
    # later than this observation. This marker is process-local and is never
    # written back to legacy rows.
    snapshot_observed_at = time.time()
    rows = [
        {**row, "_senex_snapshot_observed_at_epoch": snapshot_observed_at}
        for row in rows if isinstance(row, dict)
    ]
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
    *,
    decision_cutoff: Any | None = None,
) -> dict[str, Any]:
    """Replay only already-settled proof-qualified evidence into ``core``.

    ``rows`` is a pre-decision snapshot. The returned provenance records the
    number of qualified examples available at that moment, the exact selected
    IDs (latest 50 maximum), and a deterministic effective-weight hash. No
    outcome from the prediction being produced can be present in this snapshot.
    """
    normalized = _normalize_symbol(symbol)
    base_weights = _reset_replay_state(core)
    cutoff_epoch = _timestamp_epoch(decision_cutoff)
    available = []
    for row in rows:
        origin_epoch = _timestamp_epoch(row.get("ts"))
        observed_epoch = _settlement_observed_epoch(row)
        horizon_elapsed = (
            cutoff_epoch is None
            or (origin_epoch is not None and origin_epoch + 3600.0 <= cutoff_epoch)
        )
        evidence_known = observed_epoch is not None and (
            cutoff_epoch is None or observed_epoch <= cutoff_epoch
        )
        if (
            _normalize_symbol(str(row.get("symbol") or "")) == normalized
            and horizon_elapsed
            and evidence_known
            and _proof_gate(row)
        ):
            available.append(row)
    available.sort(key=lambda row: (_timestamp_epoch(row.get("ts")) or float("-inf"), str(row.get("id") or "")))
    proof_qualified_raw_available_before_decision = len(available)
    try:
        from backend.authoritative_score import independent_1h_cohort
    except ImportError:
        from senecio_polymarket.backend.authoritative_score import independent_1h_cohort
    independent = independent_1h_cohort(available)
    proof_qualified_available_before_decision = len(independent)
    qualified = independent[-MAX_LEARNING_EXAMPLES:]

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
        "availability_rule": "EXPLICIT_SETTLEMENT_OR_QUERY_SNAPSHOT_OBSERVED_AT_OR_BEFORE_DECISION",
        "proof_qualified_available_before_decision": proof_qualified_available_before_decision,
        "proof_qualified_raw_available_before_decision": proof_qualified_raw_available_before_decision,
        "authority_cohort": "INDEPENDENT_NONOVERLAP_1H",
        "authority_n_field": "proof_qualified_n",
        "min_examples": MIN_LEARNING_EXAMPLES,
        "max_replayed_examples": MAX_LEARNING_EXAMPLES,
        "proof_qualified_n": len(qualified),
        "wins": wins,
        "losses": losses,
        "source_prediction_ids": [row.get("id") for row in qualified],
        "source_evidence_hash": _evidence_hash(qualified),
        "decision_cutoff_epoch": cutoff_epoch,
        "learning_snapshot_cutoff_epoch": cutoff_epoch,
        "authority_horizon_seconds": 3600,
        "code_hash": _code_hash(),
        "config_hash": _config_hash(base_weights),
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
        self._authoritative_learning_loaded_monotonic: float | None = None
        self._authoritative_learning_state: dict[str, Any] = {
            "version": LEARNING_VERSION,
            "learning_version": LEARNING_VERSION,
            "status": "NOT_LOADED",
            "proof_qualified_n": 0,
            "proof_qualified_available_before_decision": 0,
            "evidence_cut": "PRE_DECISION_SNAPSHOT",
            "uses_only_prior_settled_evidence": True,
            "source_prediction_ids": [],
            "source_evidence_hash": _evidence_hash([]),
            "code_hash": _code_hash(),
            "config_hash": _config_hash(self._senex_base_weights),
            "effective_weights_hash": effective_weights_hash(self.weights),
        }

    def _load_learning_for_symbol(self, symbol: str, decision_cutoff: Any | None = None) -> None:
        normalized = _normalize_symbol(symbol)
        now = time.monotonic()
        if (
            self._authoritative_learning_symbol == normalized
            and self._authoritative_learning_loaded_monotonic is not None
            and now - self._authoritative_learning_loaded_monotonic <= FETCH_CACHE_TTL_S
        ):
            return
        self._authoritative_learning_symbol = normalized
        self._authoritative_learning_loaded_monotonic = now
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
                "source_evidence_hash": _evidence_hash([]),
                "code_hash": _code_hash(),
                "config_hash": _config_hash(self._senex_base_weights),
                "effective_weights": _weights_payload(self.weights),
                "effective_weights_hash": effective_weights_hash(self.weights),
            }
            return
        try:
            candidates = fetch_authoritative_rows(normalized)
            effective_cutoff = decision_cutoff
            if effective_cutoff is None:
                effective_cutoff = datetime.now(timezone.utc)
            self._authoritative_learning_state = replay_authoritative_learning(
                self, candidates, normalized, decision_cutoff=effective_cutoff
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
                "source_evidence_hash": _evidence_hash([]),
                "code_hash": _code_hash(),
                "config_hash": _config_hash(self._senex_base_weights),
                "effective_weights": _weights_payload(self.weights),
                "effective_weights_hash": effective_weights_hash(self.weights),
            }

    def ingest_market(self, market: dict) -> dict:
        """Attach provenance and mask unavailable inputs at the runtime bridge."""
        state = super().ingest_market(market)
        supplied = market.get("feature_observations") or {}
        features = (
            "orderflow", "volume_delta", "bidask_imbalance",
            "funding_signal", "oi_momentum", "price_momentum",
        )
        availability = {}
        for feature in features:
            item = supplied.get(feature) if isinstance(supplied, dict) else None
            if isinstance(item, dict) and item.get("status"):
                availability[feature] = dict(item)
                continue
            observed = True
            if feature in {"price_momentum", "volume_delta"}:
                observed = len(market.get("ohlcv") or []) >= 2
            elif feature in {"orderflow", "bidask_imbalance"}:
                book = market.get("orderbook") or {}
                observed = float(book.get("bid_depth") or 0) + float(book.get("ask_depth") or 0) > 0
            elif feature == "funding_signal":
                # Derivative values require the connector's explicit public
                # instrument provenance. A bare numeric compatibility field is
                # not sufficient evidence of observation.
                observed = False
            elif feature == "oi_momentum":
                observed = False
            value = float(state.get(feature) or 0.0)
            availability[feature] = {
                "status": ("REAL_OBSERVED_ZERO" if abs(value) <= 1e-12 else "REAL_NONZERO") if observed else "MISSING",
                "source": "legacy_explicit_input" if observed else "unavailable",
                "fallback_value": None if observed else 0.0,
            }
        observed_statuses = {"REAL_OBSERVED_ZERO", "REAL_NONZERO"}
        for feature, item in availability.items():
            if item.get("status") not in observed_statuses:
                state[feature] = 0.0
        state["feature_availability_v1"] = availability
        return state

    def compress_features(self, market_state: dict) -> dict:
        """Exclude unavailable inputs from agreement/noise semantics.

        The frozen core still supplies every other pipeline behavior. Missing
        features contribute no pressure and, unlike a measured neutral zero,
        do not participate in the signal-agreement denominator.
        """
        features = super().compress_features(market_state)
        if not isinstance(features, dict):
            return features
        availability = market_state.get("feature_availability_v1") or {}
        pressure_to_feature = {
            "orderflow": "orderflow", "volume_delta": "volume_delta",
            "bidask": "bidask_imbalance", "funding": "funding_signal",
            "oi": "oi_momentum", "price_momentum": "price_momentum",
        }
        pressures = features.get("pressures") or {}
        observed_statuses = {"REAL_OBSERVED_ZERO", "REAL_NONZERO"}
        observed_pressures = []
        masked = []
        for pressure_name, feature_name in pressure_to_feature.items():
            item = availability.get(feature_name) if isinstance(availability, dict) else None
            status = item.get("status") if isinstance(item, dict) else None
            if status in observed_statuses:
                observed_pressures.append(float(pressures.get(pressure_name) or 0.0))
            else:
                pressures[pressure_name] = None
                masked.append(feature_name)
        numeric_pressures = [value for value in pressures.values() if isinstance(value, (int, float))]
        total_pressure = sum(numeric_pressures)
        positive_count = sum(value > 0 for value in observed_pressures)
        negative_count = sum(value < 0 for value in observed_pressures)
        if observed_pressures:
            agreement = max(positive_count, negative_count) / len(observed_pressures)
            noise = _clamp(0.05 + (1.0 - agreement) * 2.0 / 3.0, 0.05, 1.0)
        else:
            agreement = 0.0
            noise = 1.0
        liquidity = float(market_state.get("liquidity_quality", 1.0) or 0.0)
        if liquidity < 0.5:
            noise = _clamp(noise + (1.0 - liquidity) * 0.3, 0.05, 1.0)
        up = _sigmoid(total_pressure * 5.0)
        down = _sigmoid(-total_pressure * 5.0)
        conviction = _clamp(abs(up - down) * (1.0 - noise), 0.0, 1.0)
        direction = "LONG" if total_pressure > 0.05 else "SHORT" if total_pressure < -0.05 else "NEUTRAL"
        regime_4h = str(features.get("regime_4h") or "NEUTRAL")
        long_suppressed = False
        if direction == "LONG" and regime_4h == "BEAR" and conviction < self._long_bear_bypass_conviction:
            direction = "NEUTRAL"
            long_suppressed = True
        features.update({
            "direction": direction,
            "conviction": round(conviction, 6),
            "noise": round(noise, 6),
            "total_pressure": round(total_pressure, 6),
            "up_prob": round(up, 6),
            "down_prob": round(down, 6),
            "agreement": round(agreement, 6),
            "pressures": pressures,
            "long_suppressed_by_regime": long_suppressed,
            "missing_input_mask_v1": {
                "version": "missing-input-mask-v1",
                "observed_input_count": len(observed_pressures),
                "masked_features": sorted(masked),
                "missing_excluded_from_agreement_denominator": True,
            },
        })
        return features

    def decide(self, market: dict, risk_state: dict, execution_state: dict) -> dict:
        # Learning snapshot is loaded before the prediction decision.
        self._load_learning_for_symbol(str(market.get("symbol") or ""))
        action_vector = super().decide(market, risk_state, execution_state)
        pipeline = action_vector.setdefault("pipeline", {})
        step2 = pipeline.setdefault("step2_features", {})
        if isinstance(step2, dict):
            decision_state = dict(self._authoritative_learning_state)
            decision_state["decision_cutoff_epoch"] = self._authoritative_learning_state.get("decision_cutoff_epoch")
            step2["learning_state_v1"] = decision_state
        return action_vector
