"""
SENECIO H-011 — VWAP Cross-Leg Arbitrage Detector V2
=====================================================
Versión 2 con correcciones metodológicas post-FASE 0.6.

CORRECCIONES V2 (vs V1):
  1. LOOK-AHEAD BIAS FIX: El VWAP para la ventana [t-W, t) SOLO usa trades con
     timestamp estrictamente menor que t. Para un scan en now, la ventana es
     [now-W, now) y se descartan trades con ts >= now. Garantiza que no se use
     información futura.
  2. DEV_SIGNED: Calcula dev_signed = (VWAP_YES + VWAP_NO) - 1.0 además de
     dev_abs = |dev_signed|. Permite distinguir overpriced (+) de underpriced (-).
  3. MULTI_VENTANA: --window W (segundos). Sensibilidad W ∈ {60, 120, 300, 600, 1200, 1800, 3600}.
  4. ESTIMADOR EWMA: --estimator {vwap, ewma}. EWMA con half-life = window.
  5. DEDUP: Elimina duplicados por transactionHash dentro de cada ventana.
  6. PAGINATION CLIENT-SIDE: limit+offset + dedup + filtro temporal client-side
     (los params `conditionId=`, `after=`, `before=` del endpoint son silenciosamente
     ignorados, lección FASE 0.5).

PRE-REGISTRO H-011 (inmutable):
  - umbral_deteccion = 0.02 (2 centavos)
  - umbral_sostenido = 0.05 (5 centavos)
  - exclusion = leg > 0.95
  - ventana default = 300s (5 min, elegida tras FASE 0.6 como balance sensibilidad/estabilidad)
  - criterio FASE_0 día 8: ≥5 mercados con dev_abs ≥ 0.05 en ≥3 scans distintos → FASE_1
                            caso contrario → ARCHIVAR

REGLAS FASE_0 ABSOLUTAS:
  - NO órdenes de compra/venta
  - NO modificar estado en Polymarket
  - NO tocar oracle crypto
  - NO mezclar con H-010 (archivado)
  - SOLO detector de lectura

MODO DE USO:
  # Scan one-shot (default):
  python3 vwap_detector_v2.py --max-markets 30 --window 300

  # Modo monitor para cron cada 15min:
  python3 vwap_detector_v2.py --mode monitor --window 300

  # Estimador EWMA:
  python3 vwap_detector_v2.py --window 300 --estimator ewma

Dependencies: httpx, numpy (stdlib + httpx + numpy only)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Iterable

import httpx
import numpy as np

# Reuse existing connector for Gamma market fetching
sys.path.insert(0, str(Path(__file__).parent))
from polymarket_connector import GAMMA_BASE, fetch_all_active_markets

# ═══════════════════════════════════════════════════════════════════════
# Configuration (PRE-REGISTRADA — inmutable para H-011)
# ═══════════════════════════════════════════════════════════════════════

DATA_API_BASE = "https://data-api.polymarket.com"

# Umbrales pre-registrados (NO modificar sin invalidar pre-registro H-011)
THRESHOLD_DETECTION = 0.02   # 2 centavos — flag inicial
THRESHOLD_SUSTAINED = 0.05   # 5 centavos — justifica FASE_1
EXCLUDE_LEG_ABOVE = 0.95     # mercados ya resueltos

# Ventana por defecto (definitiva tras FASE 0.6 + validación V2)
DEFAULT_WINDOW_SEC = 300     # 5 minutos

# GEMINI Q7 — Staleness filter: excluir mercados donde el Δt entre avg timestamp
# YES y avg timestamp NO supere este umbral. Mitiga artefactos de microestructura
# (stale leg trades creating artificial deviations).
# Default: 60 segundos (recomendación Gemini).
STALENESS_THRESHOLD_SEC = int(os.environ.get("H011_STALENESS_THRESHOLD", "60"))

# ═══════════════════════════════════════════════════════════════════════
# H-011b Configuration (DIRECTIONAL ARBITRAGE — Dry-Run)
# Refinado por Gemini: Kelly fraccionado + depth proxy por cuello de botella
# ═══════════════════════════════════════════════════════════════════════
H011B_FEE_ESTIMATE = 0.005     # 0.5% fricción estimada (slippage + spread)
H011B_ENTRY_THRESHOLD = 1.0 - H011B_FEE_ESTIMATE  # S < 0.995
H011B_KELLY_FRACTION = 0.2     # 20% Fractional Kelly (Gemini spec)
H011B_MAX_ORDER_SIZE = 50.0    # límite absoluto por operación
H011B_DEPTH_FRACTION = 0.10    # 10% de la liquidez activa
H011B_MIN_ORDER_USDC = 1.0     # ignorar transacciones < $1
H011B_MIN_DEPTH_USDC = 1.0     # depth_limit debe ser > $1 para operar
H011B_VIRTUAL_BALANCE_INITIAL = 1000.0
H011B_LEDGER_DATA_VALIDATION = "condition_id_match_v1"
H011_IDENTITY_GATE_VERSION = "condition_id_match_v1"
H011_SUSTAINED_SEMANTICS = "current_scan_deviation_gte_5pp"

# Dry-run ledger path (definido después de RESULTS_DIR más abajo)
DRY_RUN_LEDGER = None  # se setea después de RESULTS_DIR

# Paginación client-side
PAGE_SIZE = 500              # max soportado por el endpoint
MAX_PAGES_PER_MARKET = 20    # límite de paginación por mercado (suficiente para ventanas ≤30min en mercados líquidos)
REQUEST_DELAY_SEC = 0.15     # delay entre requests para respetar Cloudflare

# Output paths
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# H-011b dry-run ledger path (ahora que RESULTS_DIR existe)
DRY_RUN_LEDGER = RESULTS_DIR / "dry_run_ledger.jsonl"


# ═══════════════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class MarketResult:
    """Resultado de VWAP para un mercado individual."""
    market: str                       # conditionId
    question: str
    timestamp_scan: str               # ISO datetime del scan
    window_s: int                     # ventana usada
    estimator: str                    # "vwap" o "ewma"
    # VWAP YES (outcomeIndex=0)
    vwap_yes: Optional[float]
    num_trades_yes: int
    # VWAP NO (outcomeIndex=1)
    vwap_no: Optional[float]
    num_trades_no: int
    # Métricas
    sum_vwap: Optional[float]         # vwap_yes + vwap_no
    dev_abs: Optional[float]          # |sum_vwap - 1.0|
    dev_signed: Optional[float]       # (sum_vwap - 1.0), +overpriced, -underpriced
    # Flags
    flagged: bool                     # dev_abs >= THRESHOLD_DETECTION
    sustained: bool                   # dev_abs >= THRESHOLD_SUSTAINED
    # Metadata
    excluded_reason: Optional[str] = None
    volume_yes: float = 0.0
    volume_no: float = 0.0
    # Sostenido según historial (debe popularse por separado leyendo _master_log)
    sustained_in_history: Optional[bool] = None


@dataclass
class ScanReport:
    """Reporte consolidado de un scan completo."""
    scan_id: str
    scan_type: str
    started_at: str
    finished_at: str
    duration_sec: float
    window_s: int
    estimator: str
    threshold_detection: float
    threshold_sustained: float
    exclude_leg_above: float
    max_markets: int
    markets_fetched: int
    binary_markets: int
    markets_scanned: int
    markets_with_trades: int
    markets_excluded_no_trades: int
    markets_excluded_resolved: int
    markets_flagged: int
    markets_sustained: int
    deviation_stats: dict
    top_deviations: list[dict]
    # "sustained" is a per-scan magnitude threshold, not persistence across scans.
    sustained_semantics: str = H011_SUSTAINED_SEMANTICS
    # New scans explicitly identify the identity-validation cohort that produced them.
    identity_gate_active: bool = True
    data_validation: str = H011_IDENTITY_GATE_VERSION
    results: list[dict] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════
# Trade Fetching (paginación client-side + dedup + filtro temporal)
# ═══════════════════════════════════════════════════════════════════════

def fetch_trades_paginated(
    condition_id: str,
    window_start_ts: int,
    now_ts: int,
    max_pages: int = MAX_PAGES_PER_MARKET,
) -> list[dict]:
    """
    Fetch trades para un mercado usando paginación client-side.

    Estrategia:
      - GET /trades?market=<CID>&limit=500&offset=N
      - El endpoint devuelve los trades más recientes primero (orden por timestamp desc)
      - Stop conditions:
        (a) HTTP 400 (offset excede límite del endpoint)
        (b) Página devuelve < PAGE_SIZE trades (no hay más historia)
        (c) El primer trade (más reciente) de la página tiene ts < window_start_ts
            (toda la página está fuera de ventana)
        (d) max_pages alcanzado

    IMPORTANTE (look-ahead bias fix):
      - Se descartan trades con ts >= now_ts (no se usa información futura)
      - Se descartan trades con ts < window_start_ts (fuera de ventana)
      - Se descartan duplicados por transactionHash

    Returns: lista de trades únicos dentro de [window_start_ts, now_ts).

    Fail-closed data-integrity rule:
      The server must return conditionId == condition_id for every trade.
      If the remote filter is ignored or returns the global stream, do not
      attribute those trades to this market or create a dry-run record.
    """
    url = f"{DATA_API_BASE}/trades"
    all_trades_by_hash: dict[str, dict] = {}
    expected_condition_id = condition_id.lower()
    mismatched_trades = 0

    try:
        with httpx.Client(timeout=15.0) as c:
            for page in range(max_pages):
                offset = page * PAGE_SIZE
                params = {
                    "market": condition_id,
                    "limit": PAGE_SIZE,
                    "offset": offset,
                }
                r = c.get(url, params=params)
                if r.status_code != 200:
                    break  # HTTP 400 = offset excede límite, no más historia
                data = r.json()
                if not isinstance(data, list) or not data:
                    break

                returned_condition_ids = {
                    str(t.get("conditionId", "")).lower()
                    for t in data
                    if t.get("conditionId")
                }
                if expected_condition_id not in returned_condition_ids:
                    print(
                        "    [data-api] REJECTED market filter mismatch "
                        f"requested={condition_id[:18]}... "
                        f"returned={next(iter(returned_condition_ids), 'none')[:18]}..."
                    )
                    break

                # Verificar si toda la página está fuera de ventana
                # El endpoint devuelve trades ordenados por timestamp desc
                # El primer trade de la página es el más reciente
                page_max_ts = max(t.get("timestamp", 0) for t in data)
                page_min_ts = min(t.get("timestamp", 0) for t in data)

                # Si el trade más antiguo de la página ya es más reciente que now_ts,
                # toda la página está "en el futuro" — skip pero continuamos paginando
                # (no debería pasar si now_ts es el tiempo actual)
                # Si el trade más reciente de la página ya es anterior al window_start,
                # toda la página está fuera de ventana — stop
                if page_max_ts < window_start_ts:
                    break

                # Filtrar trades dentro de [window_start_ts, now_ts)
                for t in data:
                    if str(t.get("conditionId", "")).lower() != expected_condition_id:
                        mismatched_trades += 1
                        continue
                    ts = t.get("timestamp", 0)
                    if not isinstance(ts, (int, float)):
                        continue
                    if ts < window_start_ts:
                        continue
                    if ts >= now_ts:
                        continue  # look-ahead bias fix
                    tx = t.get("transactionHash")
                    if not tx:
                        continue
                    if tx in all_trades_by_hash:
                        continue  # dedup
                    all_trades_by_hash[tx] = t

                # Si la página devolvió < PAGE_SIZE, no hay más historia
                if len(data) < PAGE_SIZE:
                    break

                time.sleep(REQUEST_DELAY_SEC)

    except (httpx.TimeoutException, httpx.HTTPError) as e:
        print(f"    [data-api] Error fetching trades for {condition_id[:18]}...: {e}")

    if mismatched_trades:
        print(
            f"    [data-api] Rejected {mismatched_trades} trades with a foreign conditionId "
            f"for {condition_id[:18]}..."
        )
    return list(all_trades_by_hash.values())


# ═══════════════════════════════════════════════════════════════════════
# Estimadores
# ═══════════════════════════════════════════════════════════════════════

def compute_vwap(trades: list[dict]) -> tuple[Optional[float], int, float, Optional[float], int, float]:
    """
    VWAP estándar: sum(price * size) / sum(size), agrupado por outcomeIndex.
    Returns (vwap_yes, n_yes, vol_yes, vwap_no, n_no, vol_no).
    """
    yes_ps = yes_s = 0.0
    no_ps = no_s = 0.0
    n_yes = n_no = 0

    for t in trades:
        try:
            p = float(t.get("price", 0))
            s = float(t.get("size", 0))
            idx = int(t.get("outcomeIndex", -1))
            if p <= 0 or s <= 0 or idx not in (0, 1):
                continue
            if idx == 0:
                yes_ps += p * s
                yes_s += s
                n_yes += 1
            else:
                no_ps += p * s
                no_s += s
                n_no += 1
        except (ValueError, TypeError):
            continue

    vwap_yes = round(yes_ps / yes_s, 6) if yes_s > 0 else None
    vwap_no = round(no_ps / no_s, 6) if no_s > 0 else None
    return (vwap_yes, n_yes, round(yes_s, 4), vwap_no, n_no, round(no_s, 4))


def compute_ewma(
    trades: list[dict],
    half_life_sec: int,
    evaluation_ts: Optional[int] = None,
) -> tuple[Optional[float], int, float, Optional[float], int, float]:
    """
    EWMA (Exponentially Weighted Moving Average) con half-life = half_life_sec.

    Para cada trade, el peso es:
      w = 0.5 ^ ((t_eval - t_trade) / half_life_sec)

    EWMA = sum(price * size * w) / sum(size * w)

    GEMINI FIX (Q3 Issue A): El timestamp de referencia (t_now) NO debe ser
    el max trade timestamp (eso hace que trades stale en mercados ilíquidos
    tengan peso 1.0). Debe ser evaluation_ts (el now_ts del scan actual).
    Esto penaliza correctamente los trades antiguos incluso si el mercado
    está poco líquido.

    Args:
        trades: lista de trades en la ventana
        half_life_sec: tiempo para que el peso caiga a 50%
        evaluation_ts: timestamp de referencia (now_ts del scan). Si es None,
                       usa max(trades.timestamp) como fallback (comportamiento
                       legacy, NO recomendado).

    Returns (ewma_yes, n_yes, vol_yes, ewma_no, n_no, vol_no).
    """
    if not trades:
        return (None, 0, 0.0, None, 0, 0.0)

    # GEMINI FIX: usar evaluation_ts (now_ts del scan) en lugar de max(trades)
    if evaluation_ts is None:
        # Fallback legacy (NO recomendado — mantiene bug para compatibilidad)
        t_now = max(t.get("timestamp", 0) for t in trades)
    else:
        t_now = evaluation_ts

    yes_ps = yes_s = 0.0
    no_ps = no_s = 0.0
    n_yes = n_no = 0
    decay = math.log(2) / half_life_sec if half_life_sec > 0 else 0.0

    for t in trades:
        try:
            p = float(t.get("price", 0))
            s = float(t.get("size", 0))
            ts = float(t.get("timestamp", 0))
            idx = int(t.get("outcomeIndex", -1))
            if p <= 0 or s <= 0 or idx not in (0, 1):
                continue
            # Peso EWMA relativo a evaluation_ts (no al último trade)
            age = max(0.0, t_now - ts)
            w = math.exp(-decay * age)
            if idx == 0:
                yes_ps += p * s * w
                yes_s += s * w
                n_yes += 1
            else:
                no_ps += p * s * w
                no_s += s * w
                n_no += 1
        except (ValueError, TypeError):
            continue

    ewma_yes = round(yes_ps / yes_s, 6) if yes_s > 0 else None
    ewma_no = round(no_ps / no_s, 6) if no_s > 0 else None
    return (ewma_yes, n_yes, round(yes_s, 4), ewma_no, n_no, round(no_s, 4))


# ═══════════════════════════════════════════════════════════════════════
# Análisis por mercado
# ═══════════════════════════════════════════════════════════════════════

def analyze_market(
    market: dict,
    window_start_ts: int,
    now_ts: int,
    window_s: int,
    estimator: str,
) -> MarketResult:
    """
    Fetch trades + compute VWAP/EWMA + flag deviation for a single market.
    """
    snapshot = datetime.now(timezone.utc).isoformat()

    condition_id = market.get("conditionId") or market.get("condition_id") or ""
    question = (market.get("question") or "")[:200]

    result = MarketResult(
        market=condition_id,
        question=question,
        timestamp_scan=snapshot,
        window_s=window_s,
        estimator=estimator,
        vwap_yes=None, num_trades_yes=0, volume_yes=0.0,
        vwap_no=None, num_trades_no=0, volume_no=0.0,
        sum_vwap=None, dev_abs=None, dev_signed=None,
        flagged=False, sustained=False,
    )

    if not condition_id:
        result.excluded_reason = "no_condition_id"
        return result

    # Fetch trades con look-ahead bias fix
    trades = fetch_trades_paginated(condition_id, window_start_ts, now_ts)

    if not trades:
        result.excluded_reason = "no_trades_in_window"
        return result

    # Calcular estimador
    if estimator == "vwap":
        v_yes, n_yes, vol_yes, v_no, n_no, vol_no = compute_vwap(trades)
    elif estimator == "ewma":
        # GEMINI FIX Q3: pasar now_ts como evaluation_ts para que el decay
        # sea relativo al momento del scan, no al último trade
        v_yes, n_yes, vol_yes, v_no, n_no, vol_no = compute_ewma(trades, window_s, evaluation_ts=now_ts)
    else:
        result.excluded_reason = f"unknown_estimator_{estimator}"
        return result

    result.vwap_yes = v_yes
    result.num_trades_yes = n_yes
    result.volume_yes = vol_yes
    result.vwap_no = v_no
    result.num_trades_no = n_no
    result.volume_no = vol_no

    # Verificar que ambos lados tengan trades
    if n_yes < 1 or n_no < 1:
        result.excluded_reason = f"insufficient_trades_yes={n_yes}_no={n_no}"
        return result

    # GEMINI Q7 — STALENESS FILTER (microstructure artifact mitigation)
    # Descartar mercados donde el Δt entre avg timestamp YES y avg timestamp NO
    # supere STALENESS_THRESHOLD_SEC. Esto filtra mercados donde un leg tiene
    # trades frescos y el otro leg tiene trades stale (creando deviaciones
    # artificiales que NO son arbitraje ejecutable).
    yes_ts_list = [t.get("timestamp", 0) for t in trades if t.get("outcomeIndex") == 0]
    no_ts_list = [t.get("timestamp", 0) for t in trades if t.get("outcomeIndex") == 1]
    if yes_ts_list and no_ts_list:
        yes_avg_ts = sum(yes_ts_list) / len(yes_ts_list)
        no_avg_ts = sum(no_ts_list) / len(no_ts_list)
        staleness_delta = abs(yes_avg_ts - no_avg_ts)
        if staleness_delta > STALENESS_THRESHOLD_SEC:
            result.excluded_reason = (
                f"staleness_filter_delta={int(staleness_delta)}s_yes_avg={int(yes_avg_ts)}_no_avg={int(no_avg_ts)}"
            )
            return result

    # Exclusion: leg above 0.95 (already resolved)
    if v_yes > EXCLUDE_LEG_ABOVE or v_no > EXCLUDE_LEG_ABOVE:
        result.excluded_reason = f"leg_above_{EXCLUDE_LEG_ABOVE}_yes={v_yes:.4f}_no={v_no:.4f}"
        return result

    # Métricas firmadas y absolutas
    sum_vwap = v_yes + v_no
    dev_signed_val = sum_vwap - 1.0
    dev_abs_val = abs(dev_signed_val)

    result.sum_vwap = round(sum_vwap, 6)
    result.dev_abs = round(dev_abs_val, 6)
    result.dev_signed = round(dev_signed_val, 6)
    result.flagged = dev_abs_val >= THRESHOLD_DETECTION
    result.sustained = dev_abs_val >= THRESHOLD_SUSTAINED

    # ════════════════════════════════════════════════════════════════════
    # H-011b DRY-RUN LOGIC (DIRECTIONAL ARBITRAGE — Kelly refinado Gemini)
    # ════════════════════════════════════════════════════════════════════
    # Solo se ejecuta arbitraje simulado cuando:
    #   1. sum_vwap < H011B_ENTRY_THRESHOLD (0.995) → underpriced tras fees
    #   2. Staleness filter ya aplicado arriba (Δt YES-NO ≤ 60s)
    #   3. Leg > 0.95 ya excluido arriba
    # Circuit breakers adicionales: depth limit, Kelly sizing, min order
    if sum_vwap < H011B_ENTRY_THRESHOLD:
        # Edge = 1.00 - S (ventaja matemática)
        edge = 1.0 - sum_vwap

        # Depth proxy: cuello de botella del leg menos líquido (Gemini spec)
        # depth_proxy = 2 * min(vol_yes, vol_no)
        # depth_limit = depth_proxy * 0.10
        depth_proxy = 2.0 * min(vol_yes, vol_no)
        depth_limit = depth_proxy * H011B_DEPTH_FRACTION

        if depth_limit > H011B_MIN_DEPTH_USDC:
            # Kelly sizing: base_size = balance * KELLY_FRACTION * edge
            virtual_balance = get_current_virtual_balance()
            base_size = virtual_balance * H011B_KELLY_FRACTION * edge
            order_size = min(base_size, depth_limit, H011B_MAX_ORDER_SIZE)

            if order_size >= H011B_MIN_ORDER_USDC:
                # PnL simulado: size * ((1.0 / S) - 1.0)
                # Neto de fee ya implícito en el umbral S < 0.995
                # PnL simulado con slippage penalty (Gemini Criterio A)
                # PnL teórico: size * ((1.0 / S) - 1.0)
                # Slippage penalty: 0.2% por leg × 2 legs = 0.4% del size
                H011B_SLIPPAGE_PENALTY = 0.004  # 0.4% total
                pnl_gross = order_size * ((1.0 / sum_vwap) - 1.0)
                pnl = pnl_gross - (order_size * H011B_SLIPPAGE_PENALTY)
                log_dry_run_trade(
                    condition_id=condition_id,
                    question=question,
                    price_yes=v_yes,
                    price_no=v_no,
                    sum_vwap=sum_vwap,
                    edge=edge,
                    size=order_size,
                    pnl=pnl,
                    timestamp=snapshot,
                )

    return result


# ═══════════════════════════════════════════════════════════════════════
# H-011b Dry-Run Ledger
# ═══════════════════════════════════════════════════════════════════════

def get_current_virtual_balance() -> float:
    """
    Lee el balance virtual actual acumulado del dry_run_ledger.
    Balance inicial = $1000 USDC + PnL acumulado de todos los trades.
    """
    base_balance = H011B_VIRTUAL_BALANCE_INITIAL
    if not DRY_RUN_LEDGER.exists():
        return base_balance
    try:
        accumulated_pnl = 0.0
        with open(DRY_RUN_LEDGER, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trade = json.loads(line)
                    if trade.get("data_validation") != H011B_LEDGER_DATA_VALIDATION:
                        continue
                    accumulated_pnl += float(trade.get("pnl", 0.0))
                except json.JSONDecodeError:
                    continue
                except (TypeError, ValueError):
                    continue
        return round(base_balance + accumulated_pnl, 4)
    except OSError:
        return base_balance


def log_dry_run_trade(
    condition_id: str,
    question: str,
    price_yes: float,
    price_no: float,
    sum_vwap: float,
    edge: float,
    size: float,
    pnl: float,
    timestamp: str,
) -> None:
    """
    Escribe una línea en dry_run_ledger.jsonl (append-only).
    Cada línea representa un trade simulado de arbitraje H-011b.

    Esquema JSON estricto (según spec Gemini + columnas extra):
    {"timestamp": "ISO-8601", "condition_id": "str", "question": "str",
     "price_yes": float, "price_no": float, "sum": float, "edge": float,
     "size": float, "pnl": float, "data_validation": "condition_id_match_v1"}
    """
    entry = {
        "timestamp": timestamp,
        "condition_id": condition_id,
        "question": question[:200],
        "price_yes": round(price_yes, 6),
        "price_no": round(price_no, 6),
        "sum": round(sum_vwap, 6),
        "edge": round(edge, 6),
        "size": round(size, 2),
        "pnl": round(pnl, 4),
        "data_validation": H011B_LEDGER_DATA_VALIDATION,
    }
    try:
        with open(DRY_RUN_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"    [H-011b] DRY-RUN TRADE | S={sum_vwap:.4f} edge={edge*100:.2f}% size=${size:.2f} pnl=${pnl:.4f}")
    except OSError as e:
        print(f"    [H-011b] Error writing to dry_run_ledger: {e}")


# ═══════════════════════════════════════════════════════════════════════
# Sostenido por historial
# ═══════════════════════════════════════════════════════════════════════

def get_sustained_in_history(condition_id: str, master_log_path: Path, n_scans_required: int = 3) -> bool:
    """
    Verifica si el mercado ha sido flaggeado (dev_abs >= THRESHOLD_SUSTAINED)
    en al menos n_scans_required scans distintos a lo largo del historial.

    Lee el _master_log.jsonl que contiene un summary por scan.
    NOTE: el master_log solo guarda markets_sustained count, no la lista de
    mercados. Para hacer este chequeo correctamente, habría que guardar la
    lista de markets sustained en cada scan. Por ahora devolvemos None —
    el criterio sostenido-in-history se evalúa offline al día 8.

    En FASE_0, el criterio "≥3 scans distintos" se evalúa al día 8 sobre
    todos los JSONL acumulados, no en tiempo real.
    """
    return None  # se evalúa offline


# ═══════════════════════════════════════════════════════════════════════
# Scan completo
# ═══════════════════════════════════════════════════════════════════════

def run_scan(
    max_markets: int,
    window_s: int,
    estimator: str,
    gamma_limit: int = 200,
) -> ScanReport:
    """Ejecuta un scan completo."""
    started_at = datetime.now(timezone.utc)
    scan_id = started_at.isoformat()
    start_time = time.time()

    print(f"\n{'=' * 70}")
    print(f"SENECIO H-011 — VWAP Detector V2 (FASE_0, READ-ONLY)")
    print(f"Scan ID: {scan_id}")
    print(f"Window: {window_s}s | Estimator: {estimator} | Max markets: {max_markets}")
    print(f"Threshold det: {THRESHOLD_DETECTION} | Sustained: {THRESHOLD_SUSTAINED} | "
          f"Exclude leg > {EXCLUDE_LEG_ABOVE}")
    print(f"{'=' * 70}")

    # NOW timestamp — punto de referencia para look-ahead bias fix
    # NOW es el "presente" del scan; ningún trade con ts >= now se usa
    now_ts = int(time.time())
    window_start_ts = now_ts - window_s

    print(f"\n[1] Window: [{datetime.fromtimestamp(window_start_ts, timezone.utc).isoformat()}, "
          f"{datetime.fromtimestamp(now_ts, timezone.utc).isoformat()})")
    print(f"    Look-ahead bias: trades con ts >= now_ts ({now_ts}) son descartados")

    # Step 1: Fetch active markets from Gamma
    print(f"\n[2] Fetching active markets from Gamma (limit={gamma_limit})...")
    try:
        markets = fetch_all_active_markets(limit=gamma_limit)
    except Exception as e:
        print(f"    FAILED: {e}")
        return _empty_report(scan_id, started_at, window_s, estimator, max_markets)
    print(f"    Active markets fetched: {len(markets)}")

    # Step 2: Filter for binary markets with conditionId
    binary_markets = []
    for m in markets:
        condition_id = m.get("conditionId") or m.get("condition_id")
        if not condition_id:
            continue
        prices_raw = m.get("outcomePrices", "[]")
        try:
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw
            if isinstance(prices, list) and len(prices) == 2:
                binary_markets.append(m)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue

    print(f"    Binary markets with conditionId: {len(binary_markets)}")

    # Sort by volume descending
    binary_markets.sort(
        key=lambda m: float(m.get("volumeNum", 0) or 0),
        reverse=True,
    )

    # Pre-filter: skip markets where Gamma outcomePrices > 0.95 (already resolved)
    pre_filtered = []
    skipped_resolved = 0
    for m in binary_markets:
        try:
            prices_raw = m.get("outcomePrices", "[]")
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw
            if isinstance(prices, list) and len(prices) == 2:
                p_yes = float(prices[0])
                p_no = float(prices[1])
                if p_yes > EXCLUDE_LEG_ABOVE or p_no > EXCLUDE_LEG_ABOVE:
                    skipped_resolved += 1
                    continue
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        pre_filtered.append(m)

    print(f"    Pre-filtered (Gamma p > {EXCLUDE_LEG_ABOVE}): {len(pre_filtered)} live, "
          f"{skipped_resolved} skipped as resolved")

    # === HEURÍSTICA PARA MERCADOS ACTIVOS ===
    # El pre-filter por volumen total prioriza mercados políticos de baja frecuencia.
    # Para el modo monitor, necesitamos mercados con trades EN LA VENTANA de scan.
    # Estrategia: además del top por volumen, identificamos mercados con
    # trades recientes vía el stream global de data-api, y los AGREGAMOS al inicio
    # de la lista (aunque no estén en el top por volumen).
    # NOTA: el stream global tiene ~5min de lag respecto al tiempo real,
    # así que usamos una ventana de "actividad reciente" más amplia (30min)
    # para identificar mercados activos, diferente de la ventana VWAP.
    active_markets_added = []
    if len(pre_filtered) > max_markets:
        activity_window = 1800  # 30min para detectar mercados activos (stream tiene lag)
        activity_window_start = now_ts - activity_window
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(f"{DATA_API_BASE}/trades", params={"limit": 1000})
                if r.status_code == 200:
                    stream_trades = r.json() if isinstance(r.json(), list) else []
                    # Filtrar trades dentro de la ventana de actividad (más amplia)
                    active_cids = set()
                    for t in stream_trades:
                        ts = t.get("timestamp", 0)
                        if activity_window_start <= ts < now_ts:
                            cid = t.get("conditionId")
                            if cid:
                                active_cids.add(cid)
                    if active_cids:
                        # Buscar metadata Gamma para estos active_cids
                        active_cids_in_pre = set(m.get("conditionId") for m in pre_filtered)
                        for cid in active_cids:
                            if cid not in active_cids_in_pre:
                                # Construir market stub mínimo
                                active_markets_added.append({
                                    "conditionId": cid,
                                    "question": f"[ACTIVE] {cid[:18]}...",
                                    "volumeNum": 0,
                                    "outcomePrices": "[0.5, 0.5]",  # placeholder
                                })
                        if active_markets_added:
                            print(f"    Active markets detected: {len(active_cids)} total, "
                                  f"{len(active_markets_added)} added to scan list")
        except Exception as e:
            print(f"    [data-api] Could not fetch active markets stream: {e}")

    # Combinar: active_markets_added + pre_filtered, luego tomar top max_markets
    combined_list = active_markets_added + pre_filtered
    markets_to_scan = combined_list[:max_markets]
    print(f"    Scanning top {len(markets_to_scan)} markets (active + by-volume)")

    # Step 3: Scan each market
    print(f"\n[3] Scanning markets...")
    results: list[MarketResult] = []
    for i, m in enumerate(markets_to_scan, 1):
        condition_id = m.get("conditionId", "")
        question = (m.get("question") or "")[:55]
        vol = float(m.get("volumeNum", 0) or 0)
        print(f"  [{i:3d}/{len(markets_to_scan)}] {question[:55]:<55} vol=${vol:>10.0f}", end="")

        r = analyze_market(m, window_start_ts, now_ts, window_s, estimator)
        results.append(r)

        if r.excluded_reason:
            print(f" → EXCLUDED ({r.excluded_reason[:40]})")
        elif r.dev_abs is not None:
            flag = " 🚩" if r.flagged else (" ⭐" if r.sustained else "")
            sign = "+" if r.dev_signed > 0 else ("-" if r.dev_signed < 0 else " ")
            print(f" → Y={r.vwap_yes:.4f} N={r.vwap_no:.4f} sum={r.sum_vwap:.4f} "
                  f"dev={sign}{r.dev_abs:.4f}{flag}")

        time.sleep(REQUEST_DELAY_SEC)

    # Step 4: Compile statistics
    duration = round(time.time() - start_time, 2)
    finished_at = datetime.now(timezone.utc)

    markets_with_trades = sum(1 for r in results if r.dev_abs is not None)
    markets_excluded_no_trades = sum(1 for r in results if r.excluded_reason == "no_trades_in_window")
    markets_excluded_resolved = sum(1 for r in results if r.excluded_reason and r.excluded_reason.startswith("leg_above_"))
    markets_flagged = sum(1 for r in results if r.flagged)
    markets_sustained = sum(1 for r in results if r.sustained)

    deviations = [r.dev_abs for r in results if r.dev_abs is not None]
    if deviations:
        deviations_sorted = sorted(deviations)
        n = len(deviations_sorted)
        deviation_stats = {
            "n": n,
            "min": round(min(deviations), 6),
            "max": round(max(deviations), 6),
            "mean": round(sum(deviations) / n, 6),
            "median": round(deviations_sorted[n // 2], 6),
            "p90": round(deviations_sorted[int(n * 0.9)], 6) if n >= 10 else None,
            "above_2pp": sum(1 for d in deviations if d >= 0.02),
            "above_5pp": sum(1 for d in deviations if d >= 0.05),
        }
    else:
        deviation_stats = {"n": 0}

    top = sorted(
        [r for r in results if r.dev_abs is not None],
        key=lambda r: r.dev_abs,
        reverse=True,
    )[:10]
    top_deviations = [
        {
            "market": r.market,
            "question": r.question,
            "vwap_yes": r.vwap_yes,
            "vwap_no": r.vwap_no,
            "sum_vwap": r.sum_vwap,
            "dev_abs": r.dev_abs,
            "dev_signed": r.dev_signed,
            "num_trades_yes": r.num_trades_yes,
            "num_trades_no": r.num_trades_no,
            "flagged": r.flagged,
            "sustained": r.sustained,
        }
        for r in top
    ]

    # Step 5: Save JSONL per-scan
    jsonl_path = RESULTS_DIR / f"scan_{started_at.strftime('%Y%m%d_%H%M%S')}.jsonl"
    with open(jsonl_path, "w") as f:
        for r in results:
            f.write(json.dumps(asdict(r), ensure_ascii=False, default=str) + "\n")
    print(f"\n[4] JSONL saved: {jsonl_path}")

    # Update master log (append-only)
    master_log = RESULTS_DIR / "_master_log.jsonl"
    summary_line = {
        "scan_id": scan_id,
        "timestamp_utc": finished_at.isoformat(),
        "duration_sec": duration,
        "window_s": window_s,
        "estimator": estimator,
        "markets_scanned": len(results),
        "markets_with_trades": markets_with_trades,
        "markets_flagged": markets_flagged,
        "markets_sustained": markets_sustained,
        "sustained_semantics": H011_SUSTAINED_SEMANTICS,
        "identity_gate_active": True,
        "data_validation": H011_IDENTITY_GATE_VERSION,
        "deviation_stats": deviation_stats,
        "sustained_markets": [r.market for r in results if r.sustained],  # umbral actual, no persistencia
        "flagged_markets": [r.market for r in results if r.flagged],  # para análisis día 8
        "jsonl_file": str(jsonl_path.name),
    }
    with open(master_log, "a") as f:
        f.write(json.dumps(summary_line, ensure_ascii=False, default=str) + "\n")
    print(f"    Master log updated: {master_log}")

    # Build report
    report = ScanReport(
        scan_id=scan_id,
        scan_type="H-011_VWAP_DETECTOR_V2_FASE0",
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        duration_sec=duration,
        window_s=window_s,
        estimator=estimator,
        threshold_detection=THRESHOLD_DETECTION,
        threshold_sustained=THRESHOLD_SUSTAINED,
        exclude_leg_above=EXCLUDE_LEG_ABOVE,
        max_markets=max_markets,
        markets_fetched=len(markets),
        binary_markets=len(binary_markets),
        markets_scanned=len(results),
        markets_with_trades=markets_with_trades,
        markets_excluded_no_trades=markets_excluded_no_trades,
        markets_excluded_resolved=markets_excluded_resolved,
        markets_flagged=markets_flagged,
        markets_sustained=markets_sustained,
        deviation_stats=deviation_stats,
        top_deviations=top_deviations,
        sustained_semantics=H011_SUSTAINED_SEMANTICS,
        identity_gate_active=True,
        data_validation=H011_IDENTITY_GATE_VERSION,
        results=[asdict(r) for r in results],
    )

    # Print summary
    _print_summary(report, jsonl_path, master_log)
    return report


def _empty_report(scan_id, started_at, window_s, estimator, max_markets) -> ScanReport:
    return ScanReport(
        scan_id=scan_id, scan_type="H-011_VWAP_DETECTOR_V2_FASE0",
        started_at=started_at.isoformat(), finished_at=datetime.now(timezone.utc).isoformat(),
        duration_sec=0.0, window_s=window_s, estimator=estimator,
        threshold_detection=THRESHOLD_DETECTION, threshold_sustained=THRESHOLD_SUSTAINED,
        exclude_leg_above=EXCLUDE_LEG_ABOVE, max_markets=max_markets,
        markets_fetched=0, binary_markets=0, markets_scanned=0, markets_with_trades=0,
        markets_excluded_no_trades=0, markets_excluded_resolved=0,
        markets_flagged=0, markets_sustained=0, deviation_stats={"n": 0},
        top_deviations=[], results=[],
    )


def _print_summary(report, jsonl_path, master_log):
    print(f"\n{'=' * 70}")
    print(f"SCAN SUMMARY — {report.scan_id}")
    print(f"{'=' * 70}")
    print(f"  Window: {report.window_s}s | Estimator: {report.estimator}")
    print(f"  Markets fetched (Gamma):    {report.markets_fetched}")
    print(f"  Binary markets:             {report.binary_markets}")
    print(f"  Markets scanned:            {report.markets_scanned}")
    print(f"  Markets with trades:        {report.markets_with_trades}")
    print(f"  Excluded (no trades):       {report.markets_excluded_no_trades}")
    print(f"  Excluded (leg > 0.95):      {report.markets_excluded_resolved}")
    print(f"  Flagged (dev_abs >= 2pp):   {report.markets_flagged}")
    print(f"  Umbral actual (dev_abs >= 5pp; no persistencia): {report.markets_sustained}")
    if report.deviation_stats.get("n", 0) > 0:
        print(f"\n  Deviation distribution (n={report.deviation_stats['n']}):")
        print(f"    min:    {report.deviation_stats['min']:.6f}")
        print(f"    max:    {report.deviation_stats['max']:.6f}")
        print(f"    mean:   {report.deviation_stats['mean']:.6f}")
        print(f"    median: {report.deviation_stats['median']:.6f}")
        if report.deviation_stats.get("p90") is not None:
            print(f"    p90:    {report.deviation_stats['p90']:.6f}")
        print(f"    above 2pp: {report.deviation_stats['above_2pp']}")
        print(f"    above 5pp: {report.deviation_stats['above_5pp']}")

    if report.top_deviations:
        print(f"\n  Top 5 deviations:")
        print(f"    {'Question':<40} {'VWAP_Y':>7} {'VWAP_N':>7} {'Sum':>7} {'Dev':>8} {'Flag':>5}")
        print(f"    {'-' * 80}")
        for t in report.top_deviations[:5]:
            flag = "🚩" if t["sustained"] else ("⚠" if t["flagged"] else "")
            sign = "+" if t["dev_signed"] > 0 else "-"
            print(f"    {t['question'][:38]:<38} {t['vwap_yes']:>7.4f} {t['vwap_no']:>7.4f} "
                  f"{t['sum_vwap']:>7.4f} {sign}{t['dev_abs']:>7.4f} {flag:>5}")

    print(f"\n  Duration: {report.duration_sec}s")
    print(f"  JSONL: {jsonl_path}")
    print(f"  Master log: {master_log}")
    print(f"{'=' * 70}\n")


# ═══════════════════════════════════════════════════════════════════════
# Modo monitor (para cron cada 15min)
# ═══════════════════════════════════════════════════════════════════════

def run_monitor_mode(window_s: int, estimator: str = "vwap") -> ScanReport:
    """
    Modo monitor: un solo scan sobre los top-100 mercados (excluyendo resueltos),
    sale al terminar. Diseñado para ser llamado por cron cada 15 minutos.

    Comando cron sugerido:
      */15 * * * * cd /home/z/my-project/senecio/polymarket && python3 vwap_detector_v2.py --mode monitor --window 300 >> /var/log/senecio_h011_cron.log 2>&1
    """
    print("=" * 70)
    print("SENECIO H-011 — MONITOR MODE (cron cada 15min)")
    print(f"Window: {window_s}s | Estimator: {estimator} | Top-100 markets")
    print("READ-ONLY — NO ORDERS — NO STATE CHANGES")
    print("=" * 70)

    return run_scan(
        max_markets=100,
        window_s=window_s,
        estimator=estimator,
        gamma_limit=500,  # fetchea más para tener margen tras pre-filter
    )


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="SENECIO H-011 VWAP Detector — READ-ONLY")
    parser.add_argument("--mode", choices=["scan", "monitor"], default="scan",
                        help="scan: one-shot con --max-markets; monitor: top-100 para cron")
    parser.add_argument("--max-markets", type=int, default=30,
                        help="Max markets to scan (default 30, modo scan)")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_SEC,
                        help=f"VWAP window in seconds (default {DEFAULT_WINDOW_SEC})")
    parser.add_argument("--estimator", choices=["vwap", "ewma"], default="vwap",
                        help="Estimator: vwap (default) o ewma con half-life=window")
    parser.add_argument("--gamma-limit", type=int, default=3000,
                        help="How many active markets to paginate from Gamma (default 3000). "
                             "Must be >= 3000 to find btc-updown-5m markets (which appear at "
                             "offset ~2082+ in production). Lower values will silently fail to "
                             "discover any valid directional markets.")
    parser.add_argument("--pipeline", choices=["legacy-v2", "integrity-v3"], default=None,
                        help="Pipeline version: legacy-v2 (default) or integrity-v3")
    args = parser.parse_args()

    # Pipeline selection: CLI arg → env var → default
    pipeline = args.pipeline
    if pipeline is None:
        pipeline = os.environ.get("H011_PIPELINE_VERSION", "legacy-v2")

    print(f"SENECIO H-011 — VWAP Detector")
    print(f"Pipeline: {pipeline}")
    print(f"FASE_0 — READ-ONLY — NO ORDERS — NO STATE CHANGES")

    if pipeline == "integrity-v3":
        # V3 pipeline
        from h011_v3_pipeline import (
            H011V3Config, run_scan_v3,
            HttpxDataApiClient, HttpxClobClient,
            V3_RESULTS_DIR,
        )
        from discovery_v3 import (
            discover_markets_v3,
            HttpxGammaDiscoveryClient,
            monitor_discovery_loop,
        )

        config = H011V3Config(window_s=args.window)
        config.validate()  # Assert W=300, paper_only, live_capital_locked

        print(f"V3 Config: {config}")
        print(f"Cohort: h011-v3-w300-vwap-structure-v2")
        print()

        # Fix #1: Create source health trackers BEFORE HTTP calls
        from control_plane.coverage import SourceHealthTracker, not_used_source_health

        gamma_tracker = SourceHealthTracker("gamma_metadata")
        canonical_tracker = SourceHealthTracker("gamma_canonical")
        data_api_tracker = SourceHealthTracker("data_api_trades")

        gamma_client = HttpxGammaDiscoveryClient(
            gamma_tracker=gamma_tracker,
            canonical_tracker=canonical_tracker,
        )

        def discover_cycle():
            discovery = discover_markets_v3(
                config, args.gamma_limit, gamma_client,
                max_markets=args.max_markets,
                evidence_dir=V3_RESULTS_DIR / "discovery",
                as_of_ts=datetime.now(timezone.utc).isoformat(),
            )
            evidence = discovery["evidence"]
            print(
                f"Discovery {discovery['status']}: "
                f"received={evidence['total_received']} "
                f"preliminary_btc={evidence['preliminary_btc_candidates']} "
                f"selected={evidence['selected_count']}"
            )
            print(f"Discovery rejection histogram: {evidence['rejection_histogram']}")
            return discovery

        if args.mode == "monitor":
            def process_cycle(discovery):
                now_ts = int(time.time())
                result = run_scan_v3(
                    markets=discovery["markets"],
                    now_ts=now_ts,
                    config=config,
                    data_api_client=HttpxDataApiClient(),
                    clob_client=HttpxClobClient(),
                    discovery=discovery,
                    gamma_tracker=gamma_tracker,
                    canonical_tracker=canonical_tracker,
                    data_api_tracker=data_api_tracker,
                )
                print("\n[V3] Scan complete.")
                return result

            monitor_discovery_loop(
                discover=discover_cycle,
                process=process_cycle,
                sleep=time.sleep,
                interval_s=900,
            )
        else:
            discovery = discover_cycle()
            now_ts = int(time.time())
            result = run_scan_v3(
                markets=discovery["markets"],
                now_ts=now_ts,
                config=config,
                data_api_client=HttpxDataApiClient(),
                clob_client=HttpxClobClient(),
                discovery=discovery,
                gamma_tracker=gamma_tracker,
                canonical_tracker=canonical_tracker,
                data_api_tracker=data_api_tracker,
            )
            # A completed scan with zero eligible markets is an operational success,
            # not a process failure. The snapshot and discovery status carry the
            # semantic result.
            sys.exit(0)

    else:
        # Legacy V2 pipeline
        print(f"Pre-registro: window={args.window}s, det>={THRESHOLD_DETECTION}, "
              f"sust>={THRESHOLD_SUSTAINED}, exclude_leg>{EXCLUDE_LEG_ABOVE}")
        print(f"LEGACY: H011_LEGACY_WRITE_ENABLED={H011_LEGACY_WRITE_ENABLED}")
        print()

        if args.mode == "monitor":
            report = run_monitor_mode(args.window, args.estimator)
        else:
            report = run_scan(args.max_markets, args.window, args.estimator, args.gamma_limit)

        sys.exit(0 if report.markets_scanned > 0 else 1)


if __name__ == "__main__":
    main()
