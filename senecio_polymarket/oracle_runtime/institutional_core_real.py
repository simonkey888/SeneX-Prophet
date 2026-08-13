"""REAL-market decision bridge layered on authoritative learning.

Polymarket is allowed to influence BTC direction only when the public adapter
marks the current 5-minute market fresh and eligible. The contribution is fixed,
bounded, and fully recorded in step2_features. It is intentionally not a
learnable weight in v1, preventing feedback from silently amplifying the market's
own consensus signal.

Boros remains audit/context only and is not consumed here.
"""
from __future__ import annotations

import math
from typing import Any

from oracle_runtime import institutional_core as _learning

for _name in dir(_learning):
    if _name == "SingleDecisionCore" or _name.startswith("__"):
        continue
    globals()[_name] = getattr(_learning, _name)

LearningSingleDecisionCore = _learning.SingleDecisionCore
POLYMARKET_PRESSURE_WEIGHT = 0.25


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class SingleDecisionCore(LearningSingleDecisionCore):
    """Authoritative-learning SDC plus bounded real Polymarket confirmation."""

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
        try:
            raw_pressure = float(ctx.get("directional_pressure") or 0.0)
        except (TypeError, ValueError):
            raw_pressure = 0.0
        raw_pressure = max(-1.0, min(1.0, raw_pressure)) if eligible else 0.0
        component = raw_pressure * POLYMARKET_PRESSURE_WEIGHT

        try:
            base_total = float(features.get("total_pressure") or 0.0)
        except (TypeError, ValueError):
            base_total = 0.0
        combined = base_total + component

        # Agreement reduces noise modestly; contradiction increases it. The
        # adjustment is deliberately small so Polymarket cannot dominate spot.
        try:
            noise = float(features.get("noise") or 0.05)
        except (TypeError, ValueError):
            noise = 0.05
        if eligible and abs(raw_pressure) > 1e-12 and abs(base_total) > 1e-12:
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

        # Preserve ACT XXIII's higher-timeframe LONG guard after external fusion.
        regime_4h = str(features.get("regime_4h") or "NEUTRAL")
        long_suppressed = False
        if direction == "LONG" and regime_4h == "BEAR":
            bypass = float(getattr(self, "_long_bear_bypass_conviction", 0.80))
            if conviction < bypass:
                direction = "NEUTRAL"
                long_suppressed = True

        pressures = features.get("pressures")
        if not isinstance(pressures, dict):
            pressures = {}
            features["pressures"] = pressures
        pressures["polymarket"] = round(component, 6)

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
                "version": "polymarket-pressure-v1",
                "status": ctx.get("status"),
                "eligible": eligible,
                "slug": ctx.get("slug"),
                "up_probability": ctx.get("up_probability"),
                "down_probability": ctx.get("down_probability"),
                "raw_directional_pressure": round(raw_pressure, 6),
                "fixed_weight": POLYMARKET_PRESSURE_WEIGHT,
                "pressure_component": round(component, 6),
                "seconds_to_close": ctx.get("seconds_to_close"),
                "freshness_s": ctx.get("freshness_s"),
                "ws_connected": ctx.get("ws_connected"),
            },
        })
        return features
