"""REAL-market decision bridge layered on authoritative learning.

AUD-064 safety/provenance contract:
- canonical proof-qualified evidence may be replayed only into a detached shadow core;
- production decision weights are always the frozen code-defined/base weights;
- the production learning projection reads persisted ``exchange_used`` explicitly;
- learning provenance is causal/as-of and separately reports shadow mutations;
- Polymarket real data remains fully audited and diagnostic-only by default;
- non-zero external fusion still requires the pre-existing explicit PAPER experiment flag;
- Kalshi and Boros remain audit/context only and are not consumed here.
"""
from __future__ import annotations

import hashlib
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from oracle_runtime import institutional_core as _learning

for _name in dir(_learning):
    if _name == "SingleDecisionCore" or _name.startswith("__"):
        continue
    globals()[_name] = getattr(_learning, _name)

LearningSingleDecisionCore = _learning.SingleDecisionCore
POLYMARKET_PRESSURE_WEIGHT = 0.25
POLYMARKET_EXPERIMENT_FLAG = "SENEX_PAPER_POLYMARKET_DIRECTIONAL_EXPERIMENT"
AUD064_LEARNING_VERSION = "proof-qualified-shadow-replay-v1-aud064"
SHADOW_FETCH_PROJECTION = (
    "id",
    "ts",
    "symbol",
    "prediction",
    "confidence",
    "price_now",
    "outcome",
    "audit",
    "exchange_used",
)
_shadow_fetch_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def polymarket_experiment_enabled() -> bool:
    return (os.environ.get(POLYMARKET_EXPERIMENT_FLAG) or "").strip().lower() in {"1", "true", "yes", "on"}


def _shadow_wrapper_code_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def fetch_shadow_authoritative_rows(symbol: str) -> list[dict[str, Any]]:
    """Read the persisted learning projection used by the production bridge.

    ``exchange_used`` is intentionally projected from storage. It is never
    defaulted or inferred. Canonical proof qualification remains delegated to
    ``backend.settlement_proof.is_proof_qualified`` through the shared learning
    replay.
    """
    normalized = _learning._normalize_symbol(symbol)
    if not normalized:
        return []

    now = time.monotonic()
    cached = _shadow_fetch_cache.get(normalized)
    if cached and now - cached[0] <= _learning.FETCH_CACHE_TTL_S:
        return [dict(row) for row in cached[1]]

    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY") or ""
    table = os.environ.get("SUPABASE_TABLE", "oracle_predictions")
    if not url or not key:
        return []

    params = {
        "select": ",".join(SHADOW_FETCH_PROJECTION),
        "symbol": f"eq.{normalized}",
        "outcome": "in.(WIN,LOSS)",
        "order": "ts.desc",
        "limit": str(_learning.FETCH_LIMIT),
    }
    with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        response = client.get(
            f"{url}/rest/v1/{table}",
            headers=_learning._supabase_headers(key),
            params=params,
        )
    if response.status_code != 200:
        raise RuntimeError(f"supabase_learning_http_{response.status_code}")
    data = response.json()
    rows = [dict(row) for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    _shadow_fetch_cache[normalized] = (now, [dict(row) for row in rows])
    return rows


def _selected_observation_epochs(
    rows: list[dict[str, Any]],
    source_prediction_ids: list[Any],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_id = row.get("id")
        if row_id is not None:
            by_id[str(row_id)] = row
    result: list[dict[str, Any]] = []
    for prediction_id in source_prediction_ids:
        row = by_id.get(str(prediction_id))
        result.append(
            {
                "prediction_id": prediction_id,
                "observed_at_epoch": (
                    _learning._settlement_observed_epoch(row) if row is not None else None
                ),
            }
        )
    return result


def _frozen_base_state(core: Any) -> tuple[dict[str, float], str]:
    base = dict(getattr(core, "_senex_base_weights", core.weights))
    core.weights.clear()
    core.weights.update(base)
    return _learning._weights_payload(base), _learning.effective_weights_hash(base)


def replay_shadow_only_learning(
    core: Any,
    rows: list[dict[str, Any]],
    symbol: str,
    *,
    decision_cutoff: Any | None = None,
) -> dict[str, Any]:
    """Replay canonical evidence in a detached core and freeze production state.

    The shared AUD-063 proof gate, causal cutoff, independent/non-overlap cohort,
    learning threshold, and shadow update math are reused without weakening.
    Only the mutation authority changes: any adaptive result is observational.
    """
    base_payload, base_hash = _frozen_base_state(core)

    shadow_core = LearningSingleDecisionCore()
    shadow_core._senex_base_weights = dict(getattr(core, "_senex_base_weights", core.weights))
    shadow_core.weights.clear()
    shadow_core.weights.update(shadow_core._senex_base_weights)

    raw = _learning.replay_authoritative_learning(
        shadow_core,
        rows,
        symbol,
        decision_cutoff=decision_cutoff,
    )

    source_ids = list(raw.get("source_prediction_ids") or [])
    shadow_weights = dict(raw.get("effective_weights") or base_payload)
    shadow_weights_hash = str(
        raw.get("effective_weights_hash")
        or _learning.effective_weights_hash(shadow_weights)
    )
    shadow_mutations = int(raw.get("mutations") or 0)

    # Fail closed on the production object after the detached replay.
    decision_payload, decision_hash = _frozen_base_state(core)

    state = dict(raw)
    state.update(
        {
            "version": AUD064_LEARNING_VERSION,
            "learning_version": AUD064_LEARNING_VERSION,
            "status": (
                "WARMUP_SHADOW_ONLY"
                if int(raw.get("proof_qualified_n") or 0) < _learning.MIN_LEARNING_EXAMPLES
                else "SHADOW_ONLY_FAIL_CLOSED"
            ),
            "learning_mutation_authority": "SHADOW_ONLY",
            "production_learning_mutation_enabled": False,
            "size_calibration_authority": "FROZEN_BASE_ONLY",
            "mutations": 0,
            "shadow_mutations": shadow_mutations,
            "base_weights": base_payload,
            "base_weights_hash": base_hash,
            "decision_weights": decision_payload,
            "decision_weights_hash": decision_hash,
            "effective_weights": decision_payload,
            "effective_weights_hash": decision_hash,
            "shadow_weights": shadow_weights,
            "shadow_weights_hash": shadow_weights_hash,
            "source_settlement_observation_epochs": _selected_observation_epochs(
                rows,
                source_ids,
            ),
            "learning_projection_fields": list(SHADOW_FETCH_PROJECTION),
            "exchange_used_policy": "PERSISTED_VALUE_ONLY_NO_DEFAULT_NO_INFERENCE",
            "shadow_wrapper_code_hash": _shadow_wrapper_code_hash(),
            "activation_contract": {
                "status": "NOT_AUTHORIZED_BY_AUD_064",
                "production_weight_activation": False,
                "future_activation_requires_separate_owner_aud_order": True,
            },
        }
    )
    return state


class SingleDecisionCore(LearningSingleDecisionCore):
    """Production SDC with canonical learning evidence and frozen base weights."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._senex_polymarket_context: dict[str, Any] = {}

    def _load_learning_for_symbol(self, symbol: str, decision_cutoff: Any | None = None) -> None:
        normalized = _learning._normalize_symbol(symbol)
        now = time.monotonic()
        if (
            self._authoritative_learning_symbol == normalized
            and self._authoritative_learning_loaded_monotonic is not None
            and now - self._authoritative_learning_loaded_monotonic <= _learning.FETCH_CACHE_TTL_S
        ):
            return

        self._authoritative_learning_symbol = normalized
        self._authoritative_learning_loaded_monotonic = now
        base_payload, base_hash = _frozen_base_state(self)

        if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_KEY"):
            self._authoritative_learning_state = {
                "version": AUD064_LEARNING_VERSION,
                "learning_version": AUD064_LEARNING_VERSION,
                "symbol": normalized,
                "status": "DISABLED_NO_CONFIG_SHADOW_ONLY",
                "evidence_cut": "PRE_DECISION_SNAPSHOT",
                "uses_only_prior_settled_evidence": True,
                "proof_qualified_n": 0,
                "proof_qualified_available_before_decision": 0,
                "source_prediction_ids": [],
                "source_settlement_observation_epochs": [],
                "source_evidence_hash": _learning._evidence_hash([]),
                "learning_mutation_authority": "SHADOW_ONLY",
                "production_learning_mutation_enabled": False,
                "size_calibration_authority": "FROZEN_BASE_ONLY",
                "mutations": 0,
                "shadow_mutations": 0,
                "base_weights": base_payload,
                "base_weights_hash": base_hash,
                "decision_weights": base_payload,
                "decision_weights_hash": base_hash,
                "effective_weights": base_payload,
                "effective_weights_hash": base_hash,
                "shadow_weights": base_payload,
                "shadow_weights_hash": base_hash,
                "learning_projection_fields": list(SHADOW_FETCH_PROJECTION),
                "exchange_used_policy": "PERSISTED_VALUE_ONLY_NO_DEFAULT_NO_INFERENCE",
                "shadow_wrapper_code_hash": _shadow_wrapper_code_hash(),
            }
            return

        try:
            candidates = fetch_shadow_authoritative_rows(normalized)
            effective_cutoff = decision_cutoff
            if effective_cutoff is None:
                effective_cutoff = datetime.now(timezone.utc)
            self._authoritative_learning_state = replay_shadow_only_learning(
                self,
                candidates,
                normalized,
                decision_cutoff=effective_cutoff,
            )
        except Exception as exc:
            base_payload, base_hash = _frozen_base_state(self)
            self._authoritative_learning_state = {
                "version": AUD064_LEARNING_VERSION,
                "learning_version": AUD064_LEARNING_VERSION,
                "symbol": normalized,
                "status": "UNAVAILABLE_SHADOW_ONLY",
                "error": type(exc).__name__,
                "evidence_cut": "PRE_DECISION_SNAPSHOT",
                "uses_only_prior_settled_evidence": True,
                "proof_qualified_n": 0,
                "proof_qualified_available_before_decision": 0,
                "source_prediction_ids": [],
                "source_settlement_observation_epochs": [],
                "source_evidence_hash": _learning._evidence_hash([]),
                "learning_mutation_authority": "SHADOW_ONLY",
                "production_learning_mutation_enabled": False,
                "size_calibration_authority": "FROZEN_BASE_ONLY",
                "mutations": 0,
                "shadow_mutations": 0,
                "base_weights": base_payload,
                "base_weights_hash": base_hash,
                "decision_weights": base_payload,
                "decision_weights_hash": base_hash,
                "effective_weights": base_payload,
                "effective_weights_hash": base_hash,
                "shadow_weights": base_payload,
                "shadow_weights_hash": base_hash,
                "learning_projection_fields": list(SHADOW_FETCH_PROJECTION),
                "exchange_used_policy": "PERSISTED_VALUE_ONLY_NO_DEFAULT_NO_INFERENCE",
                "shadow_wrapper_code_hash": _shadow_wrapper_code_hash(),
            }

    def decide(self, market: dict, risk_state: dict, execution_state: dict) -> dict:
        # Predictions are much farther apart than the learning layer's 60s read
        # cache. Clearing the symbol memo forces a fresh pre-decision snapshot
        # for each new PAPER prediction without unbounded duplicate reads.
        self._authoritative_learning_symbol = None
        return super().decide(market, risk_state, execution_state)

    def ingest_market(self, market: dict) -> dict:
        ctx = market.get("polymarket_context")
        self._senex_polymarket_context = dict(ctx) if isinstance(ctx, dict) else {}
        return super().ingest_market(market)

    def compress_features(self, market_state: dict) -> dict:
        features = super().compress_features(market_state)
        if not isinstance(features, dict):
            return features
        ctx = self._senex_polymarket_context
        eligible = bool(ctx.get("eligible_for_prediction"))
        experiment_enabled = polymarket_experiment_enabled()
        try:
            raw_pressure = float(ctx.get("directional_pressure") or 0.0)
        except (TypeError, ValueError):
            raw_pressure = 0.0
        raw_pressure = max(-1.0, min(1.0, raw_pressure)) if eligible else 0.0
        effective_weight = POLYMARKET_PRESSURE_WEIGHT if (eligible and experiment_enabled) else 0.0
        component = raw_pressure * effective_weight
        try:
            base_total = float(features.get("total_pressure") or 0.0)
        except (TypeError, ValueError):
            base_total = 0.0
        pressures = features.get("pressures")
        if not isinstance(pressures, dict):
            pressures = {}
            features["pressures"] = pressures
        pressures["polymarket"] = round(component, 6)
        if effective_weight == 0.0:
            features["base_total_pressure"] = round(base_total, 6)
            features["polymarket_context_v1"] = {
                "version": "polymarket-pressure-v2", "status": ctx.get("status"),
                "eligible": eligible, "slug": ctx.get("slug"),
                "up_probability": ctx.get("up_probability"), "down_probability": ctx.get("down_probability"),
                "raw_directional_pressure": round(raw_pressure, 6),
                "configured_weight": POLYMARKET_PRESSURE_WEIGHT, "effective_weight": 0.0,
                "pressure_component": 0.0, "directional_use": False, "experiment_enabled": False,
                "experiment_flag": POLYMARKET_EXPERIMENT_FLAG, "seconds_to_close": ctx.get("seconds_to_close"),
                "freshness_s": ctx.get("freshness_s"), "ws_connected": ctx.get("ws_connected"),
            }
            return features
        combined = base_total + component
        try:
            noise = float(features.get("noise") or 0.05)
        except (TypeError, ValueError):
            noise = 0.05
        if abs(raw_pressure) > 1e-12 and abs(base_total) > 1e-12:
            same_sign = (raw_pressure > 0) == (base_total > 0)
            noise = _clamp(noise - 0.05 * abs(raw_pressure), 0.05, 1.0) if same_sign else _clamp(noise + 0.10 * abs(raw_pressure), 0.05, 1.0)
        up = _sigmoid(combined * 5.0)
        down = _sigmoid(-combined * 5.0)
        conviction = _clamp(abs(up - down) * (1.0 - noise), 0.0, 1.0)
        direction = "LONG" if combined > 0.05 else "SHORT" if combined < -0.05 else "NEUTRAL"
        regime_4h = str(features.get("regime_4h") or "NEUTRAL")
        long_suppressed = False
        if direction == "LONG" and regime_4h == "BEAR":
            bypass = float(getattr(self, "_long_bear_bypass_conviction", 0.80))
            if conviction < bypass:
                direction = "NEUTRAL"
                long_suppressed = True
        features.update({
            "direction": direction, "conviction": round(conviction, 6), "noise": round(noise, 6),
            "base_total_pressure": round(base_total, 6), "total_pressure": round(combined, 6),
            "up_prob": round(up, 6), "down_prob": round(down, 6),
            "long_suppressed_by_regime": long_suppressed or bool(features.get("long_suppressed_by_regime")),
            "polymarket_context_v1": {
                "version": "polymarket-pressure-v2", "status": ctx.get("status"), "eligible": eligible,
                "slug": ctx.get("slug"), "up_probability": ctx.get("up_probability"),
                "down_probability": ctx.get("down_probability"), "raw_directional_pressure": round(raw_pressure, 6),
                "configured_weight": POLYMARKET_PRESSURE_WEIGHT, "effective_weight": effective_weight,
                "pressure_component": round(component, 6), "directional_use": True, "experiment_enabled": True,
                "experiment_flag": POLYMARKET_EXPERIMENT_FLAG, "seconds_to_close": ctx.get("seconds_to_close"),
                "freshness_s": ctx.get("freshness_s"), "ws_connected": ctx.get("ws_connected"),
            },
        })
        return features
