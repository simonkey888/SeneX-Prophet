#!/usr/bin/env python3
"""
SENECIO — PREDICT_ONLY_MODE (Oracle Auditable)
================================================

PURPOSE:
    Convertir SENECIO en Oracle auditable. Solo predicción.
    No envía órdenes. No abre posiciones. No usa testnet. No usa executor.

RULES:
    ❌ No enviar órdenes
    ❌ No abrir posiciones
    ❌ No usar testnet
    ❌ No usar executor
    ✅ Solo predicción

FLOW:
    1. Fetch live market data from Binance (public endpoints, no auth)
    2. Run SDC 6-step pipeline (ingest → compress → risk → EV → feasibility → action)
    3. Output JSON prediction
    4. Append to audit log (predictions.jsonl)

OUTPUT FORMAT:
    {
        "timestamp": "2026-06-13T12:00:00Z",
        "symbol": "ETHUSDT",
        "prediction": "LONG|SHORT|FLAT",
        "confidence": 0.00,
        "ev": 0.00,
        "price_now": 0.0,
        "price_15m_later": null,
        "outcome": null
    }

AUDIT TRAIL:
    Every prediction is appended to predictions.jsonl with full pipeline trace.
    Later, a verification pass fills price_15m_later and outcome for scoring.

USAGE:
    python3 predict_only.py --symbol ETH/USDT --timeframe 15m
    python3 predict_only.py --symbol ETH/USDT --timeframe 15m --json
    python3 predict_only.py --verify                          # Fill outcomes for past predictions
"""

import sys
import os
import json
import time
import logging
import argparse
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PIPELINE_DIR)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_PREDICTIONS_PATH = os.path.join(PIPELINE_DIR, "senecio_output", "predictions.jsonl")
COMMISSION_RATE = 0.0002      # 0.02% maker fee (for EV calculation)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("senecio.oracle")


# ═══════════════════════════════════════════════════════════════════════════
# MARKET DATA — Public Binance only, no auth, no testnet
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_EXCHANGE_FALLBACK_CHAIN = ["okx", "kraken", "gate", "mexc", "bitget"]


def fetch_market_snapshot(symbol: str, timeframe: str, exchange: str = None) -> Optional[dict]:
    """Fetch a complete market snapshot using fallback chain or single exchange.

    If exchange is None (default), uses the full 5-exchange fallback chain
    (okx -> kraken -> gate -> mexc -> bitget) for survivability.
    If a specific exchange is provided, uses only that exchange.

    No API keys needed — all endpoints are public (orderbook, ticker, OHLCV, funding, OI).

    Args:
        symbol: Trading pair (e.g. "ETH/USDT").
        timeframe: Candle interval (e.g. "15m").
        exchange: Exchange name, or None for full fallback chain.

    Returns:
        Market data dict or None on failure.
    """
    # --- Fallback chain path (default) ---
    if exchange is None:
        from exchange_connector import fetch_market_snapshot_with_fallback
        result, used = fetch_market_snapshot_with_fallback(
            symbol=symbol, timeframe=timeframe
        )
        return result

    # --- Single-exchange path (explicit --exchange) ---
    try:
        from exchange_connector import ExchangeConnector

        connector = ExchangeConnector(
            symbol=symbol,
            config={
                "exchanges": [exchange],
                "timeframe": timeframe,
                "ohlcv_limit": 100,
            },
        )

        if exchange not in connector.exchanges:
            logger.error(f"Exchange '{exchange}' not initialized")
            return None

        # Fetch all data types
        ohlcv = connector.fetch_ohlcv(exchange, timeframe=timeframe, limit=100)
        ticker = connector.fetch_ticker(exchange)
        orderbook = connector.fetch_orderbook(exchange)
        funding = connector.fetch_funding_rate(exchange)
        oi = connector.fetch_open_interest(exchange)

        # Extract candle timestamp from ohlcv
        candle_ts = ohlcv[-1][0] if ohlcv else 0

        # Build bid/ask from ticker, fallback to orderbook, fallback to ohlcv
        bid = 0.0
        ask = 0.0
        if ticker:
            bid = float(ticker.get("bid") or 0)
            ask = float(ticker.get("ask") or 0)
        if bid == 0 and orderbook:
            raw_bids = orderbook.get("bids", [])
            raw_asks = orderbook.get("asks", [])
            if raw_bids:
                bid = float(raw_bids[0][0])
            if raw_asks:
                ask = float(raw_asks[0][0])
        if bid == 0 and ohlcv:
            bid = ohlcv[-1][4]
            ask = bid

        mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0
        spread = ask - bid
        spread_pct = spread / mid if mid > 0 else 0
        spread_bps = spread_pct * 10000.0

        # Orderbook depth
        bid_depth = 0.0
        ask_depth = 0.0
        if orderbook:
            bid_depth = float(orderbook.get("bid_depth_usdt", 0) or orderbook.get("bid_depth", 0) or 0)
            ask_depth = float(orderbook.get("ask_depth_usdt", 0) or orderbook.get("ask_depth", 0) or 0)
            if bid_depth == 0 and ask_depth == 0:
                raw_bids = orderbook.get("bids", [])
                raw_asks = orderbook.get("asks", [])
                for b in (raw_bids or [])[:20]:
                    if len(b) >= 2:
                        bid_depth += float(b[1])
                for a in (raw_asks or [])[:20]:
                    if len(a) >= 2:
                        ask_depth += float(a[1])

        # Funding
        funding_rate = 0.0
        next_funding_ms = 0
        if funding:
            funding_rate = float(funding.get("rate") or 0)
            next_funding_ms = int(funding.get("next_funding_ms") or 0)

        # Open interest
        oi_value = 0.0
        oi_change_pct = 0.0
        if oi:
            oi_value = float(oi.get("oi_value") or 0)
            oi_change_pct = float(oi.get("oi_change_24h_pct") or 0)

        # Volume from ticker
        volume_24h = float(ticker.get("quote_volume", 0) or 0) if ticker else 0

        # Liquidity quality
        liquidity_quality = max(0.0, 1.0 - spread_pct * 100)

        market_data = {
            "symbol": symbol,
            "timeframe": timeframe,
            "ohlcv": ohlcv or [],
            "ticker": {
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "spread": round(spread, 2),
                "spread_pct": round(spread_pct, 8),
                "spread_bps": round(spread_bps, 2),
                "volume_24h": round(volume_24h, 2),
            },
            "orderbook": {
                "bid_depth": round(bid_depth, 4),
                "ask_depth": round(ask_depth, 4),
                "spread": round(spread, 2),
            },
            "funding": {
                "rate": funding_rate,
                "next_funding_ms": next_funding_ms,
                "predicted_rate": 0.0,
            },
            "open_interest": {
                "oi_value": oi_value,
                "oi_change_24h_pct": oi_change_pct,
            },
            "timestamp": int(time.time() * 1000),
            "candle_ts": candle_ts,
            "liquidity_quality": round(liquidity_quality, 4),
        }

        try:
            connector.close()
        except Exception:
            pass

        return market_data

    except Exception as e:
        logger.error(f"fetch_market_snapshot failed ({exchange}): {e}")
        import traceback
        traceback.print_exc()
        return None


# ═══════════════════════════════════════════════════════════════════════════
# SURVIVABILITY FEEDER — Feed prediction outcomes as proxy trades
# ═══════════════════════════════════════════════════════════════════════════

def _feed_survivability_from_predictions(survivability) -> None:
    """Feed verified prediction outcomes into SurvivabilityFunction.

    Oracle mode never executes real trades, so survivability defaults to
    ruin_prob=0.50 (insufficient data) → size_factor=0.5 → all signals die.
    This reads predictions.jsonl, extracts verified outcomes (CORRECT/WRONG),
    and feeds them as proxy PnL so survivability computes real estimates.
    """
    pred_path = os.path.join(PIPELINE_DIR, "senecio_output", "predictions.jsonl")
    if not os.path.exists(pred_path):
        return

    fed_count = 0
    try:
        with open(pred_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    pred = json.loads(line)
                except json.JSONDecodeError:
                    continue
                outcome = pred.get("outcome")
                if outcome not in ("CORRECT", "WRONG"):
                    continue
                realized = pred.get("realized_return")
                if realized is None:
                    # Estimate: CORRECT ≈ +0.3%, WRONG ≈ -0.3%
                    realized = 0.003 if outcome == "CORRECT" else -0.003
                survivability.record_trade(realized)
                fed_count += 1
    except Exception:
        pass  # Non-critical: if we can't feed, survivability still works with defaults


# ═══════════════════════════════════════════════════════════════════════════
# CALIBRATION FEEDER — Feed verified outcomes to SDC calibration window (Action 4)
# ═══════════════════════════════════════════════════════════════════════════

def _feed_calibration_from_predictions(sdc) -> None:
    """Feed verified directional prediction outcomes into SDC calibration window.

    Reads predictions.jsonl, extracts verified outcomes (CORRECT/WRONG),
    and feeds them to sdc.record_outcome() for the rolling 50-trade
    calibration window. This enables adaptive size scaling based on
    actual prediction accuracy.
    """
    pred_path = os.path.join(PIPELINE_DIR, "senecio_output", "predictions.jsonl")
    if not os.path.exists(pred_path):
        return

    try:
        # Read last 50 verified directional predictions
        verified = []
        with open(pred_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    pred = json.loads(line)
                except json.JSONDecodeError:
                    continue
                outcome = pred.get("outcome")
                if outcome in ("CORRECT", "WRONG"):
                    verified.append(outcome == "CORRECT")

        # Feed only the last 50 (matches deque maxlen)
        for correct in verified[-50:]:
            sdc.record_outcome(correct)
    except Exception:
        pass  # Non-critical


# ═══════════════════════════════════════════════════════════════════════════
# SDC PIPELINE — Pure prediction, no execution
# ═══════════════════════════════════════════════════════════════════════════

def run_prediction(market_data: dict) -> dict:
    """Run the SDC 6-step pipeline and produce a prediction.

    PURE FUNCTION: Same market_data = same prediction.
    No side effects. No orders. No state mutation.

    Args:
        market_data: Market snapshot from fetch_market_snapshot().

    Returns:
        Prediction dict matching the Oracle output format.
    """
    from institutional_core import SingleDecisionCore

    # ── Initialize SDC with production parameters ──
    # Using the same governance as institutional_core.py
    sdc = SingleDecisionCore(
        max_drawdown=0.12,
        ruin_probability_threshold=0.05,
        hard_stop=True,
        max_position_pct=0.25,
        max_leverage=1,
        min_confidence=0.40,  # PATCHED: was 0.55 → 0.40 sigmoid gate (Phase 1)
        min_ev_to_trade=0.001,
        no_trade_noise=0.60,
        initial_capital=1000.0,
    )

    # ── Oracle mode: survivability as continuous size scaler (Action 2) ──
    # Previously disabled because it returned binary size_factor=0.5 for
    # insufficient data. Now it's clamped to [0.2, 1.0] and never produces
    # a KILL verdict — only reduces size continuously.
    # If insufficient trade data → survivability defaults to factor=0.5,
    # which is clamped to 0.5 (within [0.2, 1.0]) → reasonable conservative scaling.
    # No need to disable anymore.

    # ── Action 4: Feed calibration from verified predictions ──
    # Read last 50 verified directional outcomes and feed to SDC calibration.
    _feed_calibration_from_predictions(sdc)

    # ── Risk state — fresh state for each prediction ──
    # Oracle mode: always start from clean state (no accumulated drawdown/streaks)
    risk_state = {
        "drawdown": 0.0,
        "var": 0.0,
        "loss_streak": 0,
        "capital": 1000.0,
    }

    # ── Execution state — realistic mainnet conditions ──
    ticker = market_data.get("ticker", {})
    spread_bps = ticker.get("spread_bps", 2.0)
    # Realistic mainnet slippage: max(1 bps, spread * 0.5)
    # Binance mainnet typically has 0.5-2 bps spread on ETH/USDT
    realistic_slippage_bps = max(1.0, spread_bps * 0.5)

    execution_state = {
        "liquidity_quality": market_data.get("liquidity_quality", 0.95),
        "slippage_bps": realistic_slippage_bps,
        "latency_ms": 150.0,      # Binance mainnet: ~100-200ms typical
        "spread_bps": spread_bps,
    }

    # ── Run SDC pipeline ──
    action_vector = sdc.decide(market_data, risk_state, execution_state)

    # ── Extract prediction ──
    action = action_vector.get("action", "HOLD")
    side = action_vector.get("side")

    # Map SDC action to Oracle prediction
    if action == "EXECUTE" and side in ("LONG", "SHORT"):
        prediction = side
    elif action == "KILL":
        prediction = "FLAT"  # Risk kill = stay flat
    else:
        prediction = "FLAT"  # HOLD = no directional conviction

    # ── Extract confidence and EV from pipeline ──
    pipeline = action_vector.get("pipeline", {})
    step2 = pipeline.get("step2_features", {})
    step4 = pipeline.get("step4_ev", {})

    confidence = step2.get("conviction", 0.0) if isinstance(step2, dict) else 0.0
    ev = step4.get("adjusted_ev", 0.0) if isinstance(step4, dict) else 0.0

    # ── Current price ──
    price_now = market_data.get("ticker", {}).get("bid", 0.0)
    if price_now == 0:
        ohlcv = market_data.get("ohlcv", [])
        price_now = ohlcv[-1][4] if ohlcv else 0.0

    # ── Build Oracle output ──
    symbol_raw = market_data.get("symbol", "ETH/USDT").replace("/", "")

    oracle_output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol_raw,
        "prediction": prediction,
        "confidence": round(confidence, 4),
        "ev": round(ev, 8),
        "price_now": round(price_now, 2),
        "price_15m_later": None,
        "outcome": None,
        # ── Audit trail (full pipeline) ──
        "_audit": {
            "action_vector": {
                "action": action,
                "side": side,
                "size": action_vector.get("size", 0.0),
                "reason": action_vector.get("reason", ""),
            },
            "pipeline": {
                "step1_market": pipeline.get("step1_market", {}),
                "step2_features": pipeline.get("step2_features", {}),
                "step3_risk": pipeline.get("step3_risk", {}),
                "step4_ev": pipeline.get("step4_ev", {}),
                "step5_feasibility": pipeline.get("step5_feasibility", {}),
            },
            "execution_state": execution_state,
            "candle_ts": market_data.get("candle_ts", 0),
        },
    }

    return oracle_output


# ═══════════════════════════════════════════════════════════════════════════
# AUDIT LOG — Append-only JSONL
# ═══════════════════════════════════════════════════════════════════════════

def log_prediction(prediction: dict, path: str = DEFAULT_PREDICTIONS_PATH):
    """Append prediction to JSONL audit log.

    Each line is a complete prediction record. Later, verify_predictions()
    fills price_15m_later and outcome for scoring.

    Args:
        prediction: Oracle output dict.
        path: Path to JSONL file.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(prediction, default=str) + "\n")
        logger.info(f"Prediction logged to {path}")
    except IOError as e:
        logger.error(f"Failed to log prediction: {e}")


def verify_predictions(symbol: str = "ETH/USDT", timeframe: str = "15m",
                       path: str = DEFAULT_PREDICTIONS_PATH,
                       max_age_minutes: int = 1440,
                       current_price: float = None,
                       exchange: str = None):
    """Verify past predictions by filling price_15m_later and outcome.

    For each prediction in the JSONL that has price_15m_later=null,
    check if 15 minutes have passed since the prediction timestamp.
    If so, use the provided current_price (or fetch a fresh one) and fill:
    - price_15m_later: current price at verification time
    - outcome: "CORRECT" if prediction matched, "WRONG" if not, "SKIP" if FLAT

    AUTO-VERIFY: This is called automatically before each new prediction,
    so outcomes are filled without manual --verify runs.

    Args:
        symbol: Trading pair.
        timeframe: Candle interval.
        path: Path to predictions JSONL.
        max_age_minutes: Only verify predictions within this age window (default 24h).
        current_price: If provided, skip fetching and use this price (saves API call).
    """
    if not os.path.exists(path):
        logger.info("No predictions file to verify")
        return 0

    # Read all predictions
    with open(path, "r") as f:
        lines = f.readlines()

    if not lines:
        logger.info("No predictions to verify")
        return 0

    predictions = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                predictions.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    now = datetime.now(timezone.utc)
    modified = False
    verified_count = 0

    # Use provided price or fetch fresh one
    if current_price is None or current_price <= 0:
        market = fetch_market_snapshot(symbol, timeframe, exchange=exchange)
        if market:
            current_price = market.get("ticker", {}).get("bid", 0.0)
            if current_price == 0:
                ohlcv = market.get("ohlcv", [])
                current_price = ohlcv[-1][4] if ohlcv else 0.0

    if current_price <= 0:
        logger.error("Cannot verify: failed to fetch current price")
        return 0

    for pred in predictions:
        if pred.get("price_15m_later") is not None:
            continue  # Already verified

        ts_str = pred.get("timestamp", "")
        try:
            pred_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        age_minutes = (now - pred_ts).total_seconds() / 60.0

        # Only verify if at least 15 minutes have passed and within max age
        if age_minutes < 15:
            continue
        if age_minutes > max_age_minutes:
            # Too old — mark as STALE so we don't keep trying
            pred["price_15m_later"] = round(current_price, 2)
            pred["outcome"] = "STALE"
            modified = True
            continue

        pred_direction = pred.get("prediction", "FLAT")
        price_now = pred.get("price_now", 0)

        if price_now <= 0:
            continue

        # Fill price_15m_later with the price at verification time
        pred["price_15m_later"] = round(current_price, 2)

        # Compute outcome
        price_change_pct = (current_price - price_now) / price_now if price_now > 0 else 0

        if pred_direction == "FLAT":
            pred["outcome"] = "SKIP"
        elif pred_direction == "LONG" and price_change_pct > 0:
            pred["outcome"] = "CORRECT"
        elif pred_direction == "SHORT" and price_change_pct < 0:
            pred["outcome"] = "CORRECT"
        else:
            pred["outcome"] = "WRONG"

        modified = True
        verified_count += 1
        logger.info(f"Auto-verified: {pred_direction} → {pred['outcome']} "
                     f"(price: {price_now} → {current_price}, change: {price_change_pct:+.4%})")

    if modified:
        # Rewrite the file with updated predictions
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            for pred in predictions:
                f.write(json.dumps(pred, default=str) + "\n")
        os.replace(tmp_path, path)
        logger.info(f"Predictions file updated: {verified_count} outcomes filled")

    return verified_count


def auto_verify_previous(symbol: str, timeframe: str, current_price: float,
                         path: str = DEFAULT_PREDICTIONS_PATH) -> int:
    """Auto-verify previous predictions using the current cycle's price.

    Called at the START of each predict cycle, before making a new prediction.
    This uses the current price we already fetched (no extra API call) to
    fill outcomes for any predictions that are ≥15 minutes old.

    Returns the number of predictions verified.
    """
    if not os.path.exists(path):
        return 0

    now = datetime.now(timezone.utc)

    # Read all predictions
    with open(path, "r") as f:
        lines = f.readlines()

    if not lines:
        return 0

    predictions = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                predictions.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    modified = False
    verified_count = 0

    for pred in predictions:
        if pred.get("price_15m_later") is not None:
            continue

        ts_str = pred.get("timestamp", "")
        try:
            pred_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        age_minutes = (now - pred_ts).total_seconds() / 60.0

        if age_minutes < 15:
            continue  # Not enough time has passed

        pred_direction = pred.get("prediction", "FLAT")
        price_now = pred.get("price_now", 0)

        if price_now <= 0:
            continue

        # Fill price_15m_later with the current cycle's price
        pred["price_15m_later"] = round(current_price, 2)

        # Compute outcome
        price_change_pct = (current_price - price_now) / price_now if price_now > 0 else 0

        if pred_direction == "FLAT":
            pred["outcome"] = "SKIP"
        elif pred_direction == "LONG" and price_change_pct > 0:
            pred["outcome"] = "CORRECT"
        elif pred_direction == "SHORT" and price_change_pct < 0:
            pred["outcome"] = "CORRECT"
        else:
            pred["outcome"] = "WRONG"

        modified = True
        verified_count += 1
        logger.info(f"Auto-verify: {pred_direction} → {pred['outcome']} "
                     f"({price_now} → {current_price}, {price_change_pct:+.4%})")

    if modified:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            for pred in predictions:
                f.write(json.dumps(pred, default=str) + "\n")
        os.replace(tmp_path, path)

    return verified_count


def check_candle_duplicate(candle_ts: int, path: str = DEFAULT_PREDICTIONS_PATH, symbol: str = "") -> bool:
    """Check if we already have a prediction for this candle timestamp AND symbol.

    CANDLE DEDUPLICATION: If the last closed candle's timestamp matches
    a previous prediction FOR THE SAME SYMBOL, we skip — no point predicting
    the same candle twice.

    BUGFIX (ACT XXI): Previously compared only candle_ts. Since ETH and BTC
    share the same 15m candle boundary (same unix minute / 900), BTC was
    always flagged as duplicate of ETH. Now we require both candle_ts AND
    symbol to match. Symbol is normalized (slash stripped: "ETH/USDT" → "ETHUSDT").

    Returns True if (candle_ts, symbol) pair already has a prediction.
    """
    if not os.path.exists(path) or candle_ts == 0:
        return False

    # Normalize symbol the same way the prediction stores it: "ETH/USDT" → "ETHUSDT"
    norm_sym = symbol.replace("/", "") if symbol else ""

    with open(path, "r") as f:
        lines = f.readlines()

    if not lines:
        return False

    # Only check the last few predictions (most recent first)
    for line in reversed(lines[-5:]):
        line = line.strip()
        if not line:
            continue
        try:
            pred = json.loads(line)
            pred_candle = pred.get("_audit", {}).get("candle_ts", 0)
            if pred_candle != candle_ts:
                continue
            # Same candle_ts — if symbol also matches, it's a true duplicate.
            # If symbol is unknown (legacy callers), keep old behavior (treat as dup).
            pred_sym = pred.get("symbol", "")
            if not norm_sym or not pred_sym or pred_sym == norm_sym:
                return True
            # Same candle_ts but DIFFERENT symbol — not a duplicate, keep scanning
        except json.JSONDecodeError:
            continue

    return False


def compute_oracle_score(path: str = DEFAULT_PREDICTIONS_PATH) -> dict:
    """Compute the Oracle accuracy score from verified predictions.

    Returns:
        Score dict with total, correct, wrong, accuracy, avg_confidence, etc.
    """
    if not os.path.exists(path):
        return {"total": 0, "verified": 0, "accuracy": 0.0}

    with open(path, "r") as f:
        lines = f.readlines()

    predictions = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                predictions.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    verified = [p for p in predictions if p.get("outcome") is not None and p["outcome"] != "SKIP"]
    directional = [p for p in verified if p.get("prediction") in ("LONG", "SHORT")]
    correct = [p for p in directional if p["outcome"] == "CORRECT"]
    wrong = [p for p in directional if p["outcome"] == "WRONG"]

    accuracy = len(correct) / len(directional) if directional else 0.0

    # Confidence calibration: avg confidence of correct vs wrong
    avg_conf_correct = sum(p.get("confidence", 0) for p in correct) / len(correct) if correct else 0
    avg_conf_wrong = sum(p.get("confidence", 0) for p in wrong) / len(wrong) if wrong else 0

    # EV calibration: avg EV of correct vs wrong
    avg_ev_correct = sum(p.get("ev", 0) for p in correct) / len(correct) if correct else 0
    avg_ev_wrong = sum(p.get("ev", 0) for p in wrong) / len(wrong) if wrong else 0

    # Direction distribution
    long_count = sum(1 for p in predictions if p.get("prediction") == "LONG")
    short_count = sum(1 for p in predictions if p.get("prediction") == "SHORT")
    flat_count = sum(1 for p in predictions if p.get("prediction") == "FLAT")

    return {
        "total_predictions": len(predictions),
        "verified": len(verified),
        "directional": len(directional),
        "correct": len(correct),
        "wrong": len(wrong),
        "accuracy": round(accuracy, 4),
        "avg_confidence_correct": round(avg_conf_correct, 4),
        "avg_confidence_wrong": round(avg_conf_wrong, 4),
        "avg_ev_correct": round(avg_ev_correct, 8),
        "avg_ev_wrong": round(avg_ev_wrong, 8),
        "distribution": {
            "LONG": long_count,
            "SHORT": short_count,
            "FLAT": flat_count,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="SENECIO — PREDICT_ONLY_MODE (Oracle Auditable)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbol", type=str, default="ETH/USDT",
                        help="Trading pair (default: ETH/USDT)")
    parser.add_argument("--timeframe", type=str, default="15m",
                        help="Candle timeframe (default: 15m)")
    parser.add_argument("--exchange", type=str, default=None,
                        help="Exchange for public data (default: fallback chain okx->kraken->gate->mexc->bitget)")
    parser.add_argument("--json", action="store_true",
                        help="Output only JSON (no formatting)")
    parser.add_argument("--verify", action="store_true",
                        help="Verify past predictions (fill outcomes)")
    parser.add_argument("--score", action="store_true",
                        help="Print Oracle accuracy score")
    parser.add_argument("--log-path", type=str, default=DEFAULT_PREDICTIONS_PATH,
                        help=f"Predictions JSONL path (default: {DEFAULT_PREDICTIONS_PATH})")
    args = parser.parse_args()

    # ── Score mode ──
    if args.score:
        score = compute_oracle_score(args.log_path)
        print(json.dumps(score, indent=2))
        return 0

    # ── Verify mode ──
    if args.verify:
        verify_predictions(args.symbol, args.timeframe, args.log_path, exchange=args.exchange)
        score = compute_oracle_score(args.log_path)
        print(json.dumps(score, indent=2))
        return 0

    # ── PREDICT MODE ──

    # Step 0: Auto-verify previous predictions (fill outcomes)
    # This is the KEY fix — every cycle checks if previous predictions
    # now have enough age (>=15 min) to verify against current price.
    logger.info("Auto-verifying previous predictions...")
    n_verified = verify_predictions(args.symbol, args.timeframe, args.log_path, exchange=args.exchange)
    if n_verified > 0:
        logger.info(f"Auto-verified {n_verified} previous predictions")

    # Step 1: Fetch live market data
    exchange_desc = args.exchange or "fallback chain (okx->kraken->gate->mexc->bitget)"
    logger.info(f"Fetching market snapshot: {args.symbol} @ {args.timeframe} from {exchange_desc}")
    market_data = fetch_market_snapshot(args.symbol, args.timeframe, args.exchange)

    # Extract exchange provenance
    exchange_used = market_data.get("exchange_used", args.exchange or "NONE_ALL_FAILED") if market_data else "NONE_ALL_FAILED"

    if market_data is None:
        logger.error("Failed to fetch market data — all exchanges failed, aborting")
        print(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": args.symbol.replace("/", ""),
            "prediction": "FLAT",
            "confidence": 0.0,
            "ev": 0.0,
            "price_now": 0.0,
            "price_15m_later": None,
            "outcome": None,
            "exchange_used": "NONE_ALL_FAILED",
            "error": "MARKET_DATA_UNAVAILABLE",
        }, indent=2))
        sys.exit(1)

    # Step 1b: Candle deduplication — skip if same candle already predicted
    candle_ts = market_data.get("candle_ts", 0)
    if check_candle_duplicate(candle_ts, args.log_path, args.symbol):
        logger.info(f"Same candle already predicted (candle_ts={candle_ts}) — skipping")
        if args.json:
            print(json.dumps({"status": "DUPLICATE_CANDLE", "candle_ts": candle_ts, "action": "SKIPPED"}))
        else:
            print(f"[SKIP] Candle {candle_ts} already predicted — no new prediction needed")
        return 0

    # Step 2: Run SDC prediction pipeline
    logger.info("Running SDC prediction pipeline...")
    prediction = run_prediction(market_data)

    # Step 2b: Auto-verify using current price (no extra API call)
    current_price = prediction.get("price_now", 0)
    if current_price > 0:
        n_auto = auto_verify_previous(args.symbol, args.timeframe, current_price, args.log_path)
        if n_auto > 0:
            logger.info(f"Auto-verified {n_auto} more predictions using current price")

    # Step 2b: Add exchange provenance to prediction
    prediction["exchange_used"] = exchange_used

    # Step 3: Log to audit trail
    log_prediction(prediction, args.log_path)

    # Step 3b: Write heartbeat file
    heartbeat_path = os.path.join(os.path.dirname(args.log_path), "last_heartbeat.json")
    try:
        heartbeat = {
            "last_prediction_ts": prediction["timestamp"],
            "exchange_used": exchange_used,
            "price_now": prediction.get("price_now", 0),
            "prediction": prediction.get("prediction", "FLAT"),
        }
        with open(heartbeat_path, "w") as f:
            json.dump(heartbeat, f)
        logger.info(f"Heartbeat written to {heartbeat_path}")
    except Exception as e:
        logger.warning(f"Failed to write heartbeat: {e}")

    # Step 4: Output
    # Clean output (without _audit) for --json mode
    if args.json:
        clean = {k: v for k, v in prediction.items() if not k.startswith("_")}
        print(json.dumps(clean, indent=2))
    else:
        # Human-readable output
        sym = prediction["symbol"]
        pred = prediction["prediction"]
        conf = prediction["confidence"]
        ev = prediction["ev"]
        price = prediction["price_now"]
        ts = prediction["timestamp"]

        # Color coding
        if pred == "LONG":
            color = "\033[32m"  # Green
        elif pred == "SHORT":
            color = "\033[31m"  # Red
        else:
            color = "\033[33m"  # Yellow

        reset = "\033[0m"

        print(f"\n{'═'*72}")
        print(f"  SENECIO ORACLE — PREDICT_ONLY_MODE")
        print(f"{'═'*72}")
        print(f"  Timestamp:   {ts}")
        print(f"  Symbol:      {sym}")
        print(f"  Price Now:   ${price:,.2f}")
        print(f"  Prediction:  {color}{pred}{reset}")
        print(f"  Confidence:  {conf:.4f}")
        print(f"  EV:          {ev:.8f}")
        print(f"{'─'*72}")

        # Pipeline summary
        audit = prediction.get("_audit", {})
        pipeline = audit.get("pipeline", {})
        step2 = pipeline.get("step2_features", {})
        step3 = pipeline.get("step3_risk", {})
        step4 = pipeline.get("step4_ev", {})
        step5 = pipeline.get("step5_feasibility", {})

        if isinstance(step2, dict):
            direction = step2.get("direction", "?")
            noise = step2.get("noise", 0)
            regime = step2.get("regime_hint", "?")
            agreement = step2.get("agreement", 0)
            print(f"  Direction:   {direction}")
            print(f"  Noise:       {noise:.4f}")
            print(f"  Regime:      {regime}")
            print(f"  Agreement:   {agreement:.2%}")

        if isinstance(step3, dict):
            risk_score = step3.get("risk_score", 0)
            verdict = step3.get("verdict", "?")
            print(f"  Risk:        {risk_score:.4f} ({verdict})")

        if isinstance(step4, dict):
            tradeable = step4.get("tradeable", False)
            base_ev = step4.get("base_ev", 0)
            print(f"  EV Base:     {base_ev:.8f}")
            print(f"  Tradeable:   {tradeable}")

        if isinstance(step5, dict):
            feasible = step5.get("feasible", False)
            reason = step5.get("reason", "?")
            print(f"  Feasible:    {feasible} ({reason})")

        print(f"{'─'*72}")
        print(f"  Audit log:   {args.log_path}")
        print(f"{'═'*72}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
