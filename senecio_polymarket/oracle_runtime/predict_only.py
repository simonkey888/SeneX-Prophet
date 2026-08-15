"""Runtime bridge for the production ``predict_only`` module.

The Docker image preserves the original predictor as ``predict_only_base.py``
and installs this module at ``/app/oracle/predict_only.py``. Every original
function is re-exported unchanged except ``run_prediction``.

Production additions:
- bind the proof-qualified learning + real-market SingleDecisionCore;
- inject a bounded read-only Polymarket BTC 5m snapshot before decision time;
- attach Polymarket + Kalshi + Boros real-market evidence to prediction audit;
- label persisted confidence explicitly as raw conviction, not calibrated P(correct).

Kalshi (15m) and Boros (funding/APR) are context-only in v1 because their
horizons differ from the canonical SENEX 1h score. No trading/authentication
surface is added.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oracle_runtime import institutional_core_real as _learning_core
from backend.research.aud062_r1_contracts import feature_provenance_contract

_THIS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _THIS_DIR.parent
_ORACLE_DIR = _ROOT_DIR / "oracle"
_BASE_PATH = _ORACLE_DIR / "predict_only_base.py"
if not _BASE_PATH.exists():
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


def _poly_snapshot_for_prediction() -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).isoformat()
    try:
        from backend.polymarket_market_adapter import get_polymarket_snapshot
        raw = get_polymarket_snapshot()
    except Exception:
        return {"source": "POLYMARKET_PUBLIC", "status": "UNAVAILABLE", "observed_at": observed_at, "eligible_for_prediction": False}
    if not isinstance(raw, dict):
        return {"source": "POLYMARKET_PUBLIC", "status": "UNAVAILABLE", "observed_at": observed_at, "eligible_for_prediction": False}
    market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    up = raw.get("up") if isinstance(raw.get("up"), dict) else {}
    down = raw.get("down") if isinstance(raw.get("down"), dict) else {}
    return {
        "source": "POLYMARKET_PUBLIC",
        "version": "polymarket-btc-5m-v1",
        "status": raw.get("status"),
        "observed_at": observed_at,
        "eligible_for_prediction": bool(raw.get("eligible_for_prediction")),
        "ws_connected": bool(raw.get("ws_connected")),
        "slug": market.get("slug"),
        "condition_id": market.get("condition_id"),
        "question": market.get("question"),
        "start_ts": market.get("start_ts"),
        "end_ts": market.get("end_ts"),
        "resolution_source": market.get("resolution_source"),
        "seconds_to_close": raw.get("seconds_to_close"),
        "freshness_s": raw.get("freshness_s"),
        "up_probability": raw.get("up_probability"),
        "down_probability": raw.get("down_probability"),
        "directional_pressure": raw.get("directional_pressure"),
        "up": {
            "best_bid": up.get("best_bid"), "best_ask": up.get("best_ask"),
            "spread": up.get("spread"), "depth_imbalance": up.get("depth_imbalance"),
            "bid_depth_5": up.get("bid_depth_5"), "ask_depth_5": up.get("ask_depth_5"),
            "last_trade_price": up.get("last_trade_price"),
        },
        "down": {
            "best_bid": down.get("best_bid"), "best_ask": down.get("best_ask"),
            "spread": down.get("spread"), "depth_imbalance": down.get("depth_imbalance"),
            "bid_depth_5": down.get("bid_depth_5"), "ask_depth_5": down.get("ask_depth_5"),
            "last_trade_price": down.get("last_trade_price"),
        },
    }


def _kalshi_snapshot_for_audit() -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).isoformat()
    try:
        from backend.kalshi_market_adapter import get_kalshi_snapshot
        raw = get_kalshi_snapshot()
    except Exception:
        return {"source": "KALSHI_PUBLIC_REST", "status": "UNAVAILABLE", "observed_at": observed_at, "directional_use": False}
    if not isinstance(raw, dict):
        return {"source": "KALSHI_PUBLIC_REST", "status": "UNAVAILABLE", "observed_at": observed_at, "directional_use": False}
    return {
        "source": "KALSHI_PUBLIC_REST",
        "version": "kalshi-btc-15m-context-v1",
        "status": raw.get("status"),
        "observed_at": observed_at,
        "directional_use": False,
        "purpose": "cross_venue_prediction_market_context",
        "horizon": "15m",
        "freshness_s": raw.get("freshness_s"),
        "market": raw.get("market") if isinstance(raw.get("market"), dict) else None,
        "last_error": raw.get("last_error"),
    }


def _boros_snapshot_for_audit() -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).isoformat()
    try:
        from backend.boros_market_adapter import get_boros_snapshot
        raw = get_boros_snapshot()
    except Exception:
        return {"source": "BOROS_PUBLIC_API", "status": "UNAVAILABLE", "observed_at": observed_at, "directional_use": False}
    if not isinstance(raw, dict):
        return {"source": "BOROS_PUBLIC_API", "status": "UNAVAILABLE", "observed_at": observed_at, "directional_use": False}
    markets = raw.get("markets") if isinstance(raw.get("markets"), list) else []
    return {
        "source": "BOROS_PUBLIC_API",
        "version": "boros-funding-context-v1",
        "status": raw.get("status"),
        "observed_at": observed_at,
        "directional_use": False,
        "purpose": "funding_yield_context_only",
        "freshness_s": raw.get("freshness_s"),
        "markets": markets[:8],
        "last_error": raw.get("last_error"),
    }


def _decision_replay_snapshot(market: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Persist the bounded inputs needed by a future full-pipeline replay."""
    audit = result.get("_audit") if isinstance(result.get("_audit"), dict) else {}
    execution = audit.get("execution_state") if isinstance(audit, dict) else {}
    pipeline = audit.get("pipeline") if isinstance(audit, dict) else {}
    step2 = pipeline.get("step2_features") if isinstance(pipeline, dict) else {}
    learning = step2.get("learning_state_v1") if isinstance(step2, dict) else {}
    ohlcv = market.get("ohlcv") if isinstance(market.get("ohlcv"), list) else []
    bounded_market = {
        key: copy.deepcopy(market.get(key))
        for key in (
            "symbol", "timeframe", "ticker", "orderbook", "funding",
            "open_interest", "timestamp", "candle_ts", "liquidity_quality",
            "exchange_used", "feature_observations", "polymarket_context",
            "kalshi_context", "boros_context",
        )
    }
    bounded_market["ohlcv"] = copy.deepcopy(ohlcv[-16:])
    snapshot = {
        "version": "decision-replay-v1",
        "captured_at": result.get("timestamp"),
        "market": bounded_market,
        "risk_state": {"drawdown": 0.0, "var": 0.0, "loss_streak": 0, "capital": 1000.0},
        "execution_state": copy.deepcopy(execution) if isinstance(execution, dict) else {},
        "learning_source_prediction_ids": list(learning.get("source_prediction_ids") or []) if isinstance(learning, dict) else [],
        "learning_source_settlement_observation_epochs": (
            copy.deepcopy(learning.get("source_settlement_observation_epochs") or [])
            if isinstance(learning, dict) else []
        ),
        "learning_source_evidence_hash": learning.get("source_evidence_hash") if isinstance(learning, dict) else None,
        "effective_weights_hash": learning.get("effective_weights_hash") if isinstance(learning, dict) else None,
        "code_hash": learning.get("code_hash") if isinstance(learning, dict) else None,
        "config_hash": learning.get("config_hash") if isinstance(learning, dict) else None,
        "production_writeback": False,
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    snapshot["snapshot_hash"] = hashlib.sha256(encoded).hexdigest()
    return snapshot


def run_prediction(market_data: dict) -> dict:
    """Run the base predictor with authoritative learning + real market context."""
    working = copy.deepcopy(market_data)
    symbol = str(working.get("symbol") or "").replace("/", "").upper()
    poly = _poly_snapshot_for_prediction() if symbol == "BTCUSDT" else {
        "source": "POLYMARKET_PUBLIC",
        "version": "polymarket-btc-5m-v1",
        "status": "NOT_APPLICABLE",
        "eligible_for_prediction": False,
    }
    kalshi = _kalshi_snapshot_for_audit() if symbol == "BTCUSDT" else {
        "source": "KALSHI_PUBLIC_REST",
        "version": "kalshi-btc-15m-context-v1",
        "status": "NOT_APPLICABLE",
        "directional_use": False,
        "horizon": "15m",
    }
    boros = _boros_snapshot_for_audit()
    working["polymarket_context"] = poly
    working["kalshi_context"] = kalshi
    working["boros_context"] = boros

    previous = sys.modules.get("institutional_core")
    sys.modules["institutional_core"] = _learning_core
    try:
        result = _base.run_prediction(working)
    finally:
        if previous is None:
            sys.modules.pop("institutional_core", None)
        else:
            sys.modules["institutional_core"] = previous

    audit = result.setdefault("_audit", {})
    if isinstance(audit, dict):
        # AUD-059: confidence remains the historical persisted field, but its
        # statistical meaning is explicit and cannot be mistaken for P(correct).
        audit["confidence_semantics_v1"] = {
            "version": "confidence-semantics-v1",
            "field": "confidence",
            "source": "pipeline.step2_features.conviction",
            "semantics": "RAW_CONVICTION",
            "probability_semantics": "UNVALIDATED",
            "calibrated_probability": False,
            "brier_ece_authority_eligible": False,
        }
        audit["probability_semantics_v1"] = {
            "version": "senex-probability-semantics-v1",
            "heuristic_up_score": {"class": "HEURISTIC_DIRECTIONAL_SCORE", "calibrated_probability": False},
            "heuristic_down_score": {"class": "HEURISTIC_DIRECTIONAL_SCORE", "calibrated_probability": False},
            "up_prob": {"deprecated_alias": "heuristic_up_score", "calibrated_probability": False},
            "down_prob": {"deprecated_alias": "heuristic_down_score", "calibrated_probability": False},
            "polymarket_up_probability": {"class": "MARKET_IMPLIED_PRICE", "senex_calibrated_probability": False},
            "kalshi_yes_probability": {"class": "MARKET_IMPLIED_PRICE", "senex_calibrated_probability": False},
        }
        external = {
            "version": "real-market-context-v1",
            "polymarket": poly,
            "kalshi": kalshi,
            "boros": boros,
        }
        pipeline = audit.get("pipeline") if isinstance(audit.get("pipeline"), dict) else {}
        step2 = pipeline.get("step2_features") if isinstance(pipeline, dict) else {}
        market_up = poly.get("up_probability")
        model_up = step2.get("heuristic_up_score", step2.get("up_prob")) if isinstance(step2, dict) else None
        try:
            if market_up is not None and model_up is not None:
                external["polymarket_heuristic_score_difference_v1"] = {
                    "heuristic_up_score": round(float(model_up), 6),
                    "market_implied_up_price": round(float(market_up), 6),
                    "descriptive_difference": round(float(model_up) - float(market_up), 6),
                    "diagnostic_only": True,
                    "predictive_accuracy": False,
                    "incremental_value": "NOT_ESTIMABLE",
                }
        except (TypeError, ValueError):
            pass

        kalshi_market = kalshi.get("market") if isinstance(kalshi.get("market"), dict) else {}
        kalshi_yes = kalshi_market.get("yes_probability")
        try:
            if kalshi_yes is not None and model_up is not None:
                external["kalshi_cross_venue_descriptive_v1"] = {
                    "heuristic_up_score": round(float(model_up), 6),
                    "kalshi_15m_yes": round(float(kalshi_yes), 6),
                    "descriptive_difference": round(float(model_up) - float(kalshi_yes), 6),
                    "diagnostic_only": True,
                    "horizon_mismatch": True,
                    "predictive_accuracy": False,
                    "incremental_value": "NOT_ESTIMABLE",
                }
        except (TypeError, ValueError):
            pass
        audit["external_markets_v1"] = external
        audit["decision_replay_v1"] = _decision_replay_snapshot(working, result)
        audit["decision_provenance_roundtrip_v2"] = feature_provenance_contract(working, result)
        try:
            from backend.research.aud061_pipeline import classify_flat_reason
            audit["decision_waterfall_v1"] = {
                "version": "AUD-061-flat-waterfall-v1",
                "category": classify_flat_reason(result),
                "raw_reason": (audit.get("action_vector") or {}).get("reason"),
            }
        except Exception:
            audit["decision_waterfall_v1"] = {
                "version": "AUD-061-flat-waterfall-v1",
                "category": "OTHER_EXPLICIT_REASON",
                "raw_reason": (audit.get("action_vector") or {}).get("reason"),
            }
    return result
