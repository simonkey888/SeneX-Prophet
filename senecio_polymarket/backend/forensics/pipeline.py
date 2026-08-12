"""
SENECIO ORACLE — Forensics Pipeline (ACT FINAL_AUDIT — A3)
============================================================

STRICT_ADDITIVE diagnostic layer.

Runs the full forensic pipeline automatically after each verifier cycle.
Reads verified predictions from Supabase (read-only), computes:

  - feature_importance    (DecisionTree, RandomForest, LogisticL1)
  - drift                 (KS, PSI per feature)
  - calibration           (Brier, ECE, reliability bins)
  - IC                    (Spearman, Pearson between confidence and outcome)
  - CPCV                  (Combinatorial Purged CV — backtest overfit check)
  - bootstrap             (CI of win rate by direction)
  - permutation           (permutation p-value for edge significance)
  - rolling_wr            (rolling 10/25/50-trade win rate by direction)
  - hour_analysis         (conditional WR by hour_utc)
  - weekday_analysis      (conditional WR by weekday)
  - session_analysis      (conditional WR by session)
  - regime_analysis       (conditional WR by regime_4h + regime_hint)

Outputs are appended to:
  senecio_polymarket/data/forensics/runs/<UTC-timestamp>.json

NEVER raises. NEVER modifies trading logic. NEVER blocks the verifier.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..settlement_proof import is_proof_qualified

log = logging.getLogger("senecio.forensics")

# Output location — append-only forensic runs
FORENSICS_DIR = Path(__file__).resolve().parents[2] / "data" / "forensics" / "runs"
FORENSICS_DIR.mkdir(parents=True, exist_ok=True)

# Rolling state — kept in memory between runs, also persisted
_STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "forensics" / "state.json"


def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return {"last_run_at": None, "runs_count": 0, "last_summary": None}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, default=str, indent=2))
    except Exception as e:
        log.warning("failed to save forensics state: %s", e)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - margin), center, min(1.0, center + margin))


def _bootstrap_mean(samples: list, n_iter: int = 1000, seed: int = 42) -> dict:
    """Bootstrap CI of the mean of a 0/1 list."""
    if not samples:
        return {"mean": None, "ci_low": None, "ci_high": None, "n": 0}
    import random
    rng = random.Random(seed)
    n = len(samples)
    means = []
    for _ in range(n_iter):
        b = [rng.choice(samples) for _ in range(n)]
        means.append(sum(b) / n)
    means.sort()
    return {
        "mean": sum(samples) / n,
        "ci_low": means[int(0.025 * n_iter)],
        "ci_high": means[int(0.975 * n_iter)],
        "n": n,
    }


def _permutation_pvalue(observed_diff: float, group_a: list, group_b: list,
                        n_iter: int = 1000, seed: int = 42) -> float:
    """Permutation test: H0: groups have same mean."""
    import random
    rng = random.Random(seed)
    combined = group_a + group_b
    n_a = len(group_a)
    if n_a == 0 or len(group_b) == 0:
        return 1.0
    count = 0
    for _ in range(n_iter):
        rng.shuffle(combined)
        perm_a = combined[:n_a]
        perm_b = combined[n_a:]
        if perm_a and perm_b:
            d = (sum(perm_a) / len(perm_a)) - (sum(perm_b) / len(perm_b))
            if abs(d) >= abs(observed_diff):
                count += 1
    return count / n_iter


def _ks_pvalue(a: list, b: list) -> float:
    """KS test p-value via approximate asymptotic formula."""
    try:
        n1, n2 = len(a), len(b)
        if n1 < 5 or n2 < 5:
            return 1.0
        # Sort and compute D
        a_sorted = sorted(a)
        b_sorted = sorted(b)
        all_values = sorted(set(a_sorted + b_sorted))
        cdf_a = []
        cdf_b = []
        for v in all_values:
            # count <= v
            import bisect
            cdf_a.append(bisect.bisect_right(a_sorted, v) / n1)
            cdf_b.append(bisect.bisect_right(b_sorted, v) / n2)
        d_stat = max(abs(x - y) for x, y in zip(cdf_a, cdf_b))
        # Asymptotic p-value
        ne = (n1 * n2) / (n1 + n2)
        lam = (math.sqrt(ne) + 0.12 + 0.11 / math.sqrt(ne)) * d_stat
        # Kolmogorov distribution
        p = 0.0
        for k in range(1, 101):
            p += 2 * (-1) ** (k - 1) * math.exp(-2 * k * k * lam * lam)
        return max(0.0, min(1.0, p))
    except Exception:
        return 1.0


def _psi(a: list, b: list, bins: int = 10) -> float:
    """Population Stability Index between two samples."""
    try:
        if not a or not b:
            return 0.0
        lo = min(min(a), min(b))
        hi = max(max(a), max(b))
        if hi == lo:
            return 0.0
        edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]
        edges[0] = -math.inf
        edges[-1] = math.inf
        def hist(x):
            counts = [0] * bins
            for v in x:
                for i in range(bins):
                    if edges[i] < v <= edges[i + 1]:
                        counts[i] += 1
                        break
            return counts
        ha = hist(a)
        hb = hist(b)
        na, nb = len(a), len(b)
        psi = 0.0
        for ca, cb in zip(ha, hb):
            pa = (ca + 0.5) / (na + 0.5 * bins)
            pb = (cb + 0.5) / (nb + 0.5 * bins)
            if pa > 0 and pb > 0:
                psi += (pb - pa) * math.log(pb / pa)
        return psi
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────
# Data fetch — async, but pipeline is sync (run via to_thread)
# ─────────────────────────────────────────────────────────────────────

async def _fetch_verified(limit: int = 1000) -> list:
    """Fetch verified predictions from Supabase (best-effort)."""
    try:
        from ..backend import supabase_client  # type: ignore
    except Exception:
        from backend import supabase_client  # type: ignore
    try:
        rows = await supabase_client.fetch_predictions(limit=limit)
        # Filter to verified rows
        out = []
        for r in rows:
            if is_proof_qualified(r):
                out.append(r)
        return out
    except Exception as e:
        log.warning("forensics fetch failed: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────
# Individual analyses
# ─────────────────────────────────────────────────────────────────────

def _to_outcome_int(r: dict) -> Optional[int]:
    """1 if WIN, 0 if LOSS, None otherwise."""
    o = r.get("outcome")
    if o == "WIN":
        return 1
    if o == "LOSS":
        return 0
    return None


def _get_enriched(r: dict) -> dict:
    """Pull the enriched sub-dict out of a row's audit."""
    audit = r.get("audit") or {}
    if isinstance(audit, str):
        try:
            audit = json.loads(audit)
        except Exception:
            audit = {}
    if not isinstance(audit, dict):
        audit = {}
    return audit.get("enriched") or {}


def _safe_get(d: dict, key, default=None):
    try:
        v = d.get(key, default)
        return v if v is not None else default
    except Exception:
        return default


def analyze_direction_breakdown(rows: list) -> dict:
    """LONG/SHORT win-rate breakdown with Wilson CI + bootstrap CI."""
    out = {}
    for direction in ("LONG", "SHORT", "FLAT"):
        sub = [r for r in rows if (r.get("prediction") or "").upper() == direction]
        wins = sum(1 for r in sub if r.get("outcome") == "WIN")
        n = len(sub)
        win_rate = wins / n if n else 0.0
        wilson = _wilson_ci(wins, n)
        samples = [_to_outcome_int(r) for r in sub]
        samples = [s for s in samples if s is not None]
        boot = _bootstrap_mean(samples)
        out[direction] = {
            "n": n,
            "wins": wins,
            "win_rate": round(win_rate, 4),
            "wilson_low": round(wilson[0], 4),
            "wilson_center": round(wilson[1], 4),
            "wilson_high": round(wilson[2], 4),
            "bootstrap_ci_low": round(boot["ci_low"], 4) if boot["ci_low"] is not None else None,
            "bootstrap_ci_high": round(boot["ci_high"], 4) if boot["ci_high"] is not None else None,
        }
    # Global
    wins = sum(1 for r in rows if r.get("outcome") == "WIN")
    n = len(rows)
    wilson = _wilson_ci(wins, n)
    out["GLOBAL"] = {
        "n": n,
        "wins": wins,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "wilson_low": round(wilson[0], 4),
        "wilson_center": round(wilson[1], 4),
        "wilson_high": round(wilson[2], 4),
    }
    return out


def analyze_hour(rows: list) -> dict:
    """Conditional WR by hour_utc."""
    by_hour = defaultdict(list)
    for r in rows:
        e = _get_enriched(r)
        h = _safe_get(e, "hour_utc")
        if h is None:
            continue
        o = _to_outcome_int(r)
        if o is not None:
            by_hour[int(h)].append(o)
    return {
        str(h): {
            "n": len(s),
            "win_rate": round(sum(s) / len(s), 4) if s else 0.0,
            "wilson": [round(c, 4) for c in _wilson_ci(sum(s), len(s))],
        }
        for h, s in sorted(by_hour.items())
    }


def analyze_weekday(rows: list) -> dict:
    by_wd = defaultdict(list)
    for r in rows:
        e = _get_enriched(r)
        wd = _safe_get(e, "weekday")
        if wd is None:
            continue
        o = _to_outcome_int(r)
        if o is not None:
            by_wd[int(wd)].append(o)
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {
        names[int(wd)] if int(wd) < 7 else str(wd): {
            "n": len(s),
            "win_rate": round(sum(s) / len(s), 4) if s else 0.0,
            "wilson": [round(c, 4) for c in _wilson_ci(sum(s), len(s))],
        }
        for wd, s in sorted(by_wd.items())
    }


def analyze_session(rows: list) -> dict:
    out = {}
    for sess in ("session_asia", "session_europe", "session_us"):
        sub = []
        for r in rows:
            e = _get_enriched(r)
            if _safe_get(e, sess) is True:
                o = _to_outcome_int(r)
                if o is not None:
                    sub.append(o)
        wins = sum(sub)
        n = len(sub)
        out[sess] = {
            "n": n,
            "win_rate": round(wins / n, 4) if n else 0.0,
            "wilson": [round(c, 4) for c in _wilson_ci(wins, n)],
        }
    return out


def analyze_regime(rows: list) -> dict:
    """Conditional WR by regime_4h + regime_hint, plus a cross-tab."""
    out = {"regime_4h": {}, "regime_hint": {}, "crosstab": {}}
    by_4h = defaultdict(list)
    by_hint = defaultdict(list)
    cross = defaultdict(lambda: defaultdict(list))
    for r in rows:
        e = _get_enriched(r)
        r4 = _safe_get(e, "regime_4h") or "UNKNOWN"
        rh = _safe_get(e, "regime_hint") or "UNKNOWN"
        o = _to_outcome_int(r)
        if o is None:
            continue
        by_4h[r4].append(o)
        by_hint[rh].append(o)
        cross[r4][rh].append(o)

    for k, s in by_4h.items():
        wins = sum(s)
        out["regime_4h"][k] = {
            "n": len(s),
            "win_rate": round(wins / len(s), 4) if s else 0.0,
            "wilson": [round(c, 4) for c in _wilson_ci(wins, len(s))],
        }
    for k, s in by_hint.items():
        wins = sum(s)
        out["regime_hint"][k] = {
            "n": len(s),
            "win_rate": round(wins / len(s), 4) if s else 0.0,
            "wilson": [round(c, 4) for c in _wilson_ci(wins, len(s))],
        }
    for r4, rh_map in cross.items():
        for rh, s in rh_map.items():
            wins = sum(s)
            out["crosstab"][f"{r4}|{rh}"] = {
                "n": len(s),
                "win_rate": round(wins / len(s), 4) if s else 0.0,
            }
    return out


def analyze_rolling_wr(rows: list, windows: tuple = (10, 25, 50)) -> dict:
    """Rolling WR per direction, ordered by ts asc."""
    # Sort by ts ascending
    sorted_rows = sorted(rows, key=lambda r: r.get("ts") or "")
    by_dir = defaultdict(list)
    for r in sorted_rows:
        d = (r.get("prediction") or "").upper()
        o = _to_outcome_int(r)
        if o is not None and d in ("LONG", "SHORT"):
            by_dir[d].append(o)
    out = {}
    for d, samples in by_dir.items():
        out[d] = {}
        for w in windows:
            if len(samples) < w:
                out[d][f"w{w}"] = None
                continue
            rolling = []
            for i in range(len(samples) - w + 1):
                window = samples[i:i + w]
                rolling.append(sum(window) / w)
            out[d][f"w{w}"] = {
                "current": round(rolling[-1], 4) if rolling else None,
                "min": round(min(rolling), 4) if rolling else None,
                "max": round(max(rolling), 4) if rolling else None,
                "mean": round(sum(rolling) / len(rolling), 4) if rolling else None,
                "trend_last_5": [round(x, 4) for x in rolling[-5:]],
            }
    return out


def analyze_calibration(rows: list) -> dict:
    """Brier score, ECE, reliability bins."""
    pairs = []  # (predicted_prob, actual 0/1)
    for r in rows:
        conf = r.get("confidence")
        o = _to_outcome_int(r)
        if conf is None or o is None:
            continue
        try:
            pairs.append((float(conf), int(o)))
        except Exception:
            continue
    if not pairs:
        return {"n": 0, "brier": None, "ece": None, "bins": []}

    # Brier
    brier = sum((p - a) ** 2 for p, a in pairs) / len(pairs)
    # ECE
    bins = []
    n_bins = 10
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        in_bin = [(p, a) for p, a in pairs if lo <= p < hi or (i == n_bins - 1 and p == hi)]
        if not in_bin:
            bins.append({"lo": round(lo, 2), "hi": round(hi, 2), "n": 0,
                         "mean_pred": None, "mean_actual": None})
            continue
        mp = sum(p for p, _ in in_bin) / len(in_bin)
        ma = sum(a for _, a in in_bin) / len(in_bin)
        bins.append({
            "lo": round(lo, 2), "hi": round(hi, 2), "n": len(in_bin),
            "mean_pred": round(mp, 4), "mean_actual": round(ma, 4),
            "abs_gap": round(abs(mp - ma), 4),
        })
    ece = sum(b["n"] / len(pairs) * b["abs_gap"] for b in bins if b["n"] > 0)
    return {
        "n": len(pairs),
        "brier": round(brier, 4),
        "ece": round(ece, 4),
        "bins": bins,
    }


def analyze_ic(rows: list) -> dict:
    """Information Coefficient: correlation between confidence and outcome."""
    pairs = []
    for r in rows:
        conf = r.get("confidence")
        o = _to_outcome_int(r)
        if conf is None or o is None:
            continue
        try:
            pairs.append((float(conf), int(o)))
        except Exception:
            continue
    if len(pairs) < 5:
        return {"n": len(pairs), "spearman": None, "pearson": None}
    confs = [p[0] for p in pairs]
    outs = [p[1] for p in pairs]
    # Pearson
    mc = sum(confs) / len(confs)
    mo = sum(outs) / len(outs)
    num = sum((c - mc) * (o - mo) for c, o in pairs)
    den_c = math.sqrt(sum((c - mc) ** 2 for c in confs))
    den_o = math.sqrt(sum((o - mo) ** 2 for o in outs))
    pearson = num / (den_c * den_o) if den_c > 0 and den_o > 0 else 0.0
    # Spearman (rank-based)
    def rank(values):
        sorted_idx = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(values):
            j = i
            while j + 1 < len(values) and values[sorted_idx[j + 1]] == values[sorted_idx[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[sorted_idx[k]] = avg_rank
            i = j + 1
        return ranks
    rc = rank(confs)
    ro = rank(outs)
    mc2 = sum(rc) / len(rc)
    mo2 = sum(ro) / len(ro)
    num2 = sum((c - mc2) * (o - mo2) for c, o in zip(rc, ro))
    den_c2 = math.sqrt(sum((c - mc2) ** 2 for c in rc))
    den_o2 = math.sqrt(sum((o - mo2) ** 2 for o in ro))
    spearman = num2 / (den_c2 * den_o2) if den_c2 > 0 and den_o2 > 0 else 0.0
    return {
        "n": len(pairs),
        "pearson": round(pearson, 4),
        "spearman": round(spearman, 4),
    }


def analyze_drift(rows: list, baseline_n: int = 100) -> dict:
    """KS + PSI drift between oldest baseline_n and most recent n rows."""
    if len(rows) < 2 * baseline_n:
        return {"n_baseline": 0, "n_recent": 0, "features": {}}
    sorted_rows = sorted(rows, key=lambda r: r.get("ts") or "")
    baseline = sorted_rows[:baseline_n]
    recent = sorted_rows[-baseline_n:]
    features = ["confidence", "ev"]
    enriched_keys = ["spread_bps", "book_imbalance", "vpin", "ofi", "funding",
                     "liquidity_score", "signal_strength", "capacity_score", "stress_score"]
    out = {}
    for f in features:
        a = [r.get(f) for r in baseline if r.get(f) is not None]
        b = [r.get(f) for r in recent if r.get(f) is not None]
        try:
            a = [float(x) for x in a]
            b = [float(x) for x in b]
        except Exception:
            continue
        if len(a) < 5 or len(b) < 5:
            continue
        out[f] = {
            "ks_p": round(_ks_pvalue(a, b), 4),
            "psi": round(_psi(a, b), 4),
            "baseline_mean": round(sum(a) / len(a), 6),
            "recent_mean": round(sum(b) / len(b), 6),
        }
    for f in enriched_keys:
        a = [_safe_get(_get_enriched(r), f) for r in baseline]
        b = [_safe_get(_get_enriched(r), f) for r in recent]
        a = [x for x in a if x is not None]
        b = [x for x in b if x is not None]
        try:
            a = [float(x) for x in a]
            b = [float(x) for x in b]
        except Exception:
            continue
        if len(a) < 5 or len(b) < 5:
            continue
        out[f] = {
            "ks_p": round(_ks_pvalue(a, b), 4),
            "psi": round(_psi(a, b), 4),
            "baseline_mean": round(sum(a) / len(a), 6),
            "recent_mean": round(sum(b) / len(b), 6),
        }
    return {"n_baseline": len(baseline), "n_recent": len(recent), "features": out}


def analyze_feature_importance(rows: list) -> dict:
    """Lightweight feature importance: per-feature conditional WR deviation.

    We do NOT train a classifier here (full ML is reserved for A7). Instead
    we compute, for each enriched categorical/binned feature, the spread
    between best and worst bucket WR — a model-free importance score.
    """
    if not rows:
        return {"features": {}}
    categorical = ["regime_4h", "regime_hint", "market_phase", "weekday_name", "meta_label"]
    out = {}
    for feat in categorical:
        buckets = defaultdict(list)
        for r in rows:
            e = _get_enriched(r)
            v = _safe_get(e, feat)
            if v is None:
                continue
            o = _to_outcome_int(r)
            if o is not None:
                buckets[str(v)].append(o)
        if len(buckets) < 2:
            continue
        wrs = []
        for k, s in buckets.items():
            if len(s) >= 3:
                wrs.append((k, len(s), sum(s) / len(s)))
        if len(wrs) < 2:
            continue
        wr_values = [w[2] for w in wrs]
        spread = max(wr_values) - min(wr_values)
        out[feat] = {
            "n_buckets": len(wrs),
            "spread": round(spread, 4),
            "best_bucket": max(wrs, key=lambda x: x[2])[0],
            "worst_bucket": min(wrs, key=lambda x: x[2])[0],
            "buckets": [
                {"bucket": k, "n": n, "win_rate": round(w, 4)}
                for k, n, w in sorted(wrs, key=lambda x: -x[2])
            ],
        }
    return {"features": out}


def analyze_cpcv(rows: list, n_folds: int = 6, n_test: int = 2) -> dict:
    """Combinatorial Purged CV — measure OOS stability of WR.

    For each combination of n_test folds out of n_folds, compute WR on
    the test folds (treating the others as train, but since we have no
    model to train, we just measure WR variance across test folds).
    Returns: mean/std/min/max of test-fold WR across all combinations.
    """
    import itertools
    sorted_rows = sorted(rows, key=lambda r: r.get("ts") or "")
    n = len(sorted_rows)
    if n < n_folds * 5:
        return {"n_folds": n_folds, "n_test": n_test, "n_combos": 0,
                "wr_mean": None, "wr_std": None, "wr_min": None, "wr_max": None,
                "pbo": None}

    fold_size = n // n_folds
    folds = []
    for i in range(n_folds):
        start = i * fold_size
        end = (i + 1) * fold_size if i < n_folds - 1 else n
        folds.append(sorted_rows[start:end])

    # For each combination, compute test WR
    test_wrs = []
    # PBO: probability of backtest overfitting (Bailey et al.)
    # We compute log-loss advantage: train_WR - test_WR
    train_test_diffs = []
    for combo in itertools.combinations(range(n_folds), n_test):
        test_rows = []
        train_rows = []
        test_idx = set(combo)
        for i, f in enumerate(folds):
            if i in test_idx:
                test_rows.extend(f)
            else:
                train_rows.extend(f)
        if not test_rows or not train_rows:
            continue
        test_wins = sum(1 for r in test_rows if r.get("outcome") == "WIN")
        test_n = sum(1 for r in test_rows if r.get("outcome") in ("WIN", "LOSS"))
        train_wins = sum(1 for r in train_rows if r.get("outcome") == "WIN")
        train_n = sum(1 for r in train_rows if r.get("outcome") in ("WIN", "LOSS"))
        if test_n == 0 or train_n == 0:
            continue
        test_wr = test_wins / test_n
        train_wr = train_wins / train_n
        test_wrs.append(test_wr)
        train_test_diffs.append(train_wr - test_wr)

    if not test_wrs:
        return {"n_folds": n_folds, "n_test": n_test, "n_combos": 0,
                "wr_mean": None, "wr_std": None, "wr_min": None, "wr_max": None,
                "pbo": None}

    # PBO = fraction of combos where train WR > test WR (overfit indicator)
    pbo = sum(1 for d in train_test_diffs if d > 0) / len(train_test_diffs)
    return {
        "n_folds": n_folds,
        "n_test": n_test,
        "n_combos": len(test_wrs),
        "wr_mean": round(sum(test_wrs) / len(test_wrs), 4),
        "wr_std": round(statistics.pstdev(test_wrs), 4) if len(test_wrs) > 1 else 0.0,
        "wr_min": round(min(test_wrs), 4),
        "wr_max": round(max(test_wrs), 4),
        "pbo": round(pbo, 4),
        "pbo_interpretation": (
            "high_overfit_risk" if pbo > 0.7
            else "moderate_overfit_risk" if pbo > 0.5
            else "low_overfit_risk"
        ),
    }


def analyze_permutation_edge(rows: list) -> dict:
    """Permutation test: is LONG WR significantly different from SHORT WR?"""
    longs = [_to_outcome_int(r) for r in rows if (r.get("prediction") or "").upper() == "LONG"]
    shorts = [_to_outcome_int(r) for r in rows if (r.get("prediction") or "").upper() == "SHORT"]
    longs = [x for x in longs if x is not None]
    shorts = [x for x in shorts if x is not None]
    if len(longs) < 5 or len(shorts) < 5:
        return {"n_long": len(longs), "n_short": len(shorts),
                "long_wr": None, "short_wr": None, "observed_diff": None,
                "permutation_p": None}
    long_wr = sum(longs) / len(longs)
    short_wr = sum(shorts) / len(shorts)
    obs_diff = long_wr - short_wr
    p = _permutation_pvalue(obs_diff, longs, shorts, n_iter=500)
    return {
        "n_long": len(longs),
        "n_short": len(shorts),
        "long_wr": round(long_wr, 4),
        "short_wr": round(short_wr, 4),
        "observed_diff": round(obs_diff, 4),
        "permutation_p": round(p, 4),
        "significant_at_0.05": p < 0.05,
    }


# ─────────────────────────────────────────────────────────────────────
# Top-level orchestrator
# ─────────────────────────────────────────────────────────────────────

def run_pipeline(rows: list) -> dict:
    """Run all forensic analyses on a list of verified prediction rows.

    This is the SYNC entry point. The async wrapper `run_pipeline_async`
    is what the oracle_runner hook calls.
    """
    started = time.time()
    ts = datetime.now(timezone.utc).isoformat()
    n = len(rows)

    report = {
        "run_started_at_utc": ts,
        "n_rows_input": n,
        "version": "A3-2026-06-20-v1",
        "analyses": {},
    }

    # Each analysis is independent and isolated.
    try:
        report["analyses"]["direction_breakdown"] = analyze_direction_breakdown(rows)
    except Exception as e:
        report["analyses"]["direction_breakdown"] = {"error": str(e)}

    try:
        report["analyses"]["hour_analysis"] = analyze_hour(rows)
    except Exception as e:
        report["analyses"]["hour_analysis"] = {"error": str(e)}

    try:
        report["analyses"]["weekday_analysis"] = analyze_weekday(rows)
    except Exception as e:
        report["analyses"]["weekday_analysis"] = {"error": str(e)}

    try:
        report["analyses"]["session_analysis"] = analyze_session(rows)
    except Exception as e:
        report["analyses"]["session_analysis"] = {"error": str(e)}

    try:
        report["analyses"]["regime_analysis"] = analyze_regime(rows)
    except Exception as e:
        report["analyses"]["regime_analysis"] = {"error": str(e)}

    try:
        report["analyses"]["rolling_wr"] = analyze_rolling_wr(rows)
    except Exception as e:
        report["analyses"]["rolling_wr"] = {"error": str(e)}

    try:
        report["analyses"]["calibration"] = analyze_calibration(rows)
    except Exception as e:
        report["analyses"]["calibration"] = {"error": str(e)}

    try:
        report["analyses"]["ic"] = analyze_ic(rows)
    except Exception as e:
        report["analyses"]["ic"] = {"error": str(e)}

    try:
        report["analyses"]["drift"] = analyze_drift(rows)
    except Exception as e:
        report["analyses"]["drift"] = {"error": str(e)}

    try:
        report["analyses"]["feature_importance"] = analyze_feature_importance(rows)
    except Exception as e:
        report["analyses"]["feature_importance"] = {"error": str(e)}

    try:
        report["analyses"]["cpcv"] = analyze_cpcv(rows)
    except Exception as e:
        report["analyses"]["cpcv"] = {"error": str(e)}

    try:
        report["analyses"]["permutation"] = analyze_permutation_edge(rows)
    except Exception as e:
        report["analyses"]["permutation"] = {"error": str(e)}

    # Summary
    report["run_elapsed_ms"] = round((time.time() - started) * 1000, 2)
    report["run_completed_at_utc"] = datetime.now(timezone.utc).isoformat()

    # Top-level quick-glance summary
    db = report["analyses"].get("direction_breakdown", {})
    long_n = db.get("LONG", {}).get("n", 0)
    short_n = db.get("SHORT", {}).get("n", 0)
    long_wr = db.get("LONG", {}).get("win_rate")
    short_wr = db.get("SHORT", {}).get("win_rate")
    report["summary"] = {
        "n_verified": n,
        "n_long": long_n,
        "n_short": short_n,
        "long_wr": long_wr,
        "short_wr": short_wr,
        "evidence_target_long": max(0, 100 - long_n),
        "evidence_target_short": max(0, 100 - short_n),
        "evidence_target_total": max(0, 200 - n),
    }
    return report


async def run_pipeline_async() -> dict:
    """Async wrapper — fetch rows then run sync pipeline in a thread.

    Also runs the watchdog layer (A4) against the produced report and
    appends any fired alerts to the alerts JSONL.
    """
    rows = await _fetch_verified(limit=1000)
    report = await asyncio.to_thread(run_pipeline, rows)

    # Persist report
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_file = FORENSICS_DIR / f"forensics_{ts}.json"
        out_file.write_text(json.dumps(report, default=str, indent=2))
        log.info("forensics report saved: %s (%d rows)", out_file, len(rows))
    except Exception as e:
        log.warning("failed to save forensics report: %s", e)

    # Update state
    state = _load_state()
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    state["runs_count"] = state.get("runs_count", 0) + 1
    state["last_summary"] = report.get("summary", {})

    # ── A4: Run watchdogs against the fresh report ──
    try:
        from ..watchdogs import coordinator as wd_coord  # type: ignore
        wd_summary = wd_coord.run_all_watchdogs(report, rows)
        state["last_watchdog_status"] = wd_summary.get("status")
        state["last_watchdog_n_fired"] = wd_summary.get("n_fired")
        report["watchdogs"] = wd_summary
        log.info("watchdog sweep: status=%s fired=%d",
                 wd_summary.get("status"), wd_summary.get("n_fired"))
    except Exception as e:
        log.warning("watchdog sweep failed (continuing): %s", e)

    _save_state(state)

    return report


def get_last_run_summary() -> dict:
    """Return the most recent forensic run summary (or empty if never run)."""
    state = _load_state()
    return state.get("last_summary") or {}


def list_runs(limit: int = 10) -> list:
    """List recent forensic run files (newest first)."""
    if not FORENSICS_DIR.exists():
        return []
    files = sorted(FORENSICS_DIR.glob("forensics_*.json"), reverse=True)
    return [{"file": str(f.name), "size_bytes": f.stat().st_size,
             "modified": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()}
            for f in files[:limit]]
