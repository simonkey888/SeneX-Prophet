"""REAL-market decision bridge layered on authoritative learning.

AUD-055 safety contract:
- Polymarket real data remains available and fully audited.
- Polymarket does not change SENEX direction/conviction by default.
- Non-zero fusion requires an explicit PAPER experiment flag.
- Kalshi and Boros remain audit/context only and are not consumed here.
"""
from __future__ import annotations

import math
import os
from typing import Any

from oracle_runtime import institutional_core as _learning

for _name in dir(_learning):
    if _name == "SingleDecisionCore" or _name.startswith("__"):
        continue
    globals()[_name] = getattr(_learning, _name)

LearningSingleDecisionCore = _learning.SingleDecisionCore
POLYMARKET_PRESSURE_WEIGHT = 0.25
POLYMARKET_EXPERIMENT_FLAG = "SENEX_PAPER_POLYMARKET_DIRECTIONAL_EXPERIMENT"


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def polymarket_experiment_enabled() -> bool:
    """Explicit PAPER-only opt-in. Default is false / zero decision effect."""
    return (os.environ.get(POLYMARKET_EXPERIMENT_FLAG) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


class SingleDecisionCore(LearningSingleDecisionCore):
    """Authoritative-learning SDC with isolated Polymarket audit context."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._senex_polymarket_context: dict[str, Any] = {}

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

        # AUD-055: real Polymarket data is diagnostic-only by default.
        # The configured 0.25 research weight has NO effect unless an explicit
        # PAPER experiment opt-in is present in the runtime environment.
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

        # When the experiment is OFF, preserve the base feature result exactly.
        # No recomputation of direction, conviction, noise or probability is
        # allowed, eliminating horizon-mismatch influence on the 1h target.
        if effective_weight == 0.0:
            features["base_total_pressure"] = round(base_total, 6)
            features["polymarket_context_v1"] = {
                "version": "polymarket-pressure-v2",
                "status": ctx.get("status"),
                "eligible": eligible,
                "slug": ctx.get("slug"),
                "up_probability": ctx.get("up_probability"),
                "down_probability": ctx.get("down_probability"),
                "raw_directional_pressure": round(raw_pressure, 6),
                "configured_weight": POLYMARKET_PRESSURE_WEIGHT,
                "effective_weight": 0.0,
                "pressure_component": 0.0,
                "directional_use": False,
                "experiment_enabled": False,
                "experiment_flag": POLYMARKET_EXPERIMENT_FLAG,
                "seconds_to_close": ctx.get("seconds_to_close"),
                "freshness_s": ctx.get("freshness_s"),
                "ws_connected": ctx.get("ws_connected"),
            }
            return features

        combined = base_total + component

        # Experimental branch only. Agreement reduces noise modestly;
        # contradiction increases it. This branch is unreachable by default.
        try:
            noise = float(features.get("noise") or 0.05)
        except (TypeError, ValueError):
            noise = 0.05
        if abs(raw_pressure) > 1e-12 and abs(base_total) > 1e-12:
            same_sign = (raw_pressure > 0) == (base_total > 0)
            if same_sign:
                noise = _clamp(noise - 0.05 * abs(raw_pressure), 0.05, 1.0)
            else:
                noise = _clamp(noise + 0.10 * abs(raw_pressure), 0.05, 1.0)

        up = _sigmoid(combined * 5.0)
        down = _sigmoid(-combined * 5.0)
        conviction = _clamp(abs(up - down) * (1.0 - noise), 0.0, 1.0)
        if combined > 0.05:
            direction = "LONG"
        elif combined < -0.05:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        # Preserve ACT XXIII's higher-timeframe LONG guard after experimental fusion.
        regime_4h = str(features.get("regime_4h") or "NEUTRAL")
        long_suppressed = False
        if direction == "LONG" and regime_4h == "BEAR":
            bypass = float(getattr(self, "_long_bear_bypass_conviction", 0.80))
            if conviction < bypass:
                direction = "NEUTRAL"
                long_suppressed = True

        features.update({
            "direction": direction,
            "conviction": round(conviction, 6),
            "noise": round(noise, 6),
            "base_total_pressure": round(base_total, 6),
            "total_pressure": round(combined, 6),
            "up_prob": round(up, 6),
            "down_prob": round(down, 6),
            "long_suppressed_by_regime": long_suppressed or bool(features.get("long_suppressed_by_regime")),
            "polymarket_context_v1": {
                "version": "polymarket-pressure-v2",
                "status": ctx.get("status"),
                "eligible": eligible,
                "slug": ctx.get("slug"),
                "up_probability": ctx.get("up_probability"),
                "down_probability": ctx.get("down_probability"),
                "raw_directional_pressure": round(raw_pressure, 6),
                "configured_weight": POLYMARKET_PRESSURE_WEIGHT,
                "effective_weight": effective_weight,
                "pressure_component": round(component, 6),
                "directional_use": True,
                "experiment_enabled": True,
                "experiment_flag": POLYMARKET_EXPERIMENT_FLAG,
                "seconds_to_close": ctx.get("seconds_to_close"),
                "freshness_s": ctx.get("freshness_s"),
                "ws_connected": ctx.get("ws_connected"),
            },
        })
        return features
