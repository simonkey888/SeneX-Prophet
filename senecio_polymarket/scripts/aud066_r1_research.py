#!/usr/bin/env python3
"""AUD-066-R1 zero-cost replay: parity fail-closed + mandatory robustness matrix."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sys
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

RESEARCH_DIR = Path(__file__).resolve().parents[1] / "backend" / "research"
sys.path.insert(0, str(RESEARCH_DIR))

from aud066_liquidation import (
    normalize_liquidation,
    build_minute_market,
    make_samples,
    feature_at,
    canonical_hash,
)
from aud066_analysis import walk_forward_extended
from aud066_r1_analysis import terminal_inference, evaluate_robustness

BASE_SHA = "43c8023d3a4623381e45da02d9efa8e9b5888f47"
BASE_TREE = "20ec5775ea37a7288e8cd8748ea304843d9b0866"
DATES = [
    "2023-01-01", "2023-04-01", "2023-07-01", "2023-10-01",
    "2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01",
    "2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01",
]
DATASETS = ("trades", "quotes", "derivative_ticker", "liquidations")
MAX_BYTES = 180_000_000
ROOT = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "aud-066"

BASELINE_PARITY = "NOT_ACHIEVABLE_AT_ZERO_COST"
PARITY_MISSING_INPUTS = [
    "FULL_L2_ORDERBOOK_DEPTH_WITHIN_0_5_PERCENT_REQUIRED_BY_BASE_CONNECTOR_NOT_PRESENT_IN_TARDIS_QUOTES_TOP_OF_BOOK_DATA",
    "CONTIGUOUS_PRECEDING_24H_OPEN_INTEREST_HISTORY_REQUIRED_FOR_EXACT_OI_CHANGE_24H_PCT_NOT_PRESENT_IN_DISJOINT_FIRST_OF_MONTH_DAY_SAMPLES",
    "EXACT_PRODUCTION_INGEST_CYCLE_SEQUENCE_REQUIRED_FOR_STATEFUL_EMA5_BIDASK_BUFFER_NOT_REPRODUCIBLE_FROM_5M_DECISION_SNAPSHOTS_WITHOUT_ASSUMPTION",
    "FULL_PRODUCTION_REGIME_SUPPRESSION_STATE_AND_ITS_CAUSAL_INPUT_HISTORY_NOT_CAPTURED_BY_THE_AUD066_PUBLIC_SAMPLE_CONTRACT",
]


def dataset_url(dtype: str, date: str) -> str:
    y, m, d = date.split("-")
    symbol = "PERPETUALS" if dtype == "liquidations" else "BTCUSDT"
    return f"https://datasets.tardis.dev/v1/binance-futures/{dtype}/{y}/{m}/{d}/{symbol}.csv.gz"


def download_bounded(url: str, path: Path) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SENEX-AUD066-R1-research/1", "Accept": "application/gzip,*/*;q=.1"},
    )
    h = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=45) as response, open(path, "wb") as out:
            final = response.geturl()
            if not final.startswith("https://datasets.tardis.dev/"):
                raise RuntimeError("REDIRECT_OUTSIDE_ALLOWLIST:" + final)
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_BYTES:
                raise RuntimeError("PAYLOAD_TOO_LARGE_HEADER:" + content_length)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise RuntimeError("PAYLOAD_TOO_LARGE_STREAM:" + str(total))
                h.update(chunk)
                out.write(chunk)
        return {"url": url, "resolved_url": final, "bytes": total, "sha256": h.hexdigest(), "status": "OK"}
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        path.unlink(missing_ok=True)
        return {"url": url, "bytes": total, "sha256": None, "status": "ERROR", "error": type(exc).__name__ + ":" + str(exc)[:240]}


def csv_rows(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def filtered_rows(path: Path, counter: dict, key: str):
    for row in csv_rows(path):
        if str(row.get("symbol") or "").upper() == "BTCUSDT":
            counter[key] += 1
            yield row


def _last_state(mins: dict[int, dict], minute: int, key: str, lookback: int = 10):
    for k in range(minute, minute - lookback - 1, -1):
        value = mins.get(k, {}).get(key)
        if value is not None:
            return value
    return None


def _close(mins: dict[int, dict], minute: int):
    value = mins.get(minute, {}).get("close")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decorate_context(samples: list[dict], mins: dict[int, dict]) -> list[dict]:
    for row in samples:
        minute = row["decision_ts_us"] // 60_000_000
        quote = _last_state(mins, minute, "quote", 3)
        depth_usd = 0.0
        if quote is not None:
            _, bp, ap, ba, aa = quote
            mid = (float(bp) + float(ap)) / 2.0
            depth_usd = (float(ba) + float(aa)) * mid
        row["context"] = {
            "depth_usd": depth_usd,
            "liq_total_usd_1m": float(row["features"].get("long_liq_usd_1m", 0.0)) + float(row["features"].get("short_liq_usd_1m", 0.0)),
            "abs_oi_delta_5m": abs(float(row["features"].get("oi_delta_5m", 0.0))),
        }
    return samples


def offset_samples(day: str, mins: dict[int, dict], liqs: list[dict], offset_minutes: int) -> list[dict]:
    if not mins:
        return []
    lo = min(mins)
    hi = max(mins)
    start = ((lo + 14) // 5) * 5
    out = []
    for anchor in range(start, hi - 6, 5):
        minute = anchor + offset_minutes
        if minute < lo + 5 or minute + 5 > hi:
            continue
        t_us = (minute + 1) * 60_000_000 - 1
        features = feature_at(t_us, mins, liqs)
        p0 = _close(mins, minute)
        p5 = _close(mins, minute + 5)
        if features is None or p0 is None or p5 is None or p0 <= 0:
            continue
        out.append({
            "day": day,
            "decision_ts_us": t_us,
            "label_ts_min_us": (minute + 5) * 60_000_000,
            "y": 1 if p5 > p0 else 0,
            "features": features,
        })
    return decorate_context(out, mins)


def reconnect_fail_closed(samples: list[dict]) -> list[dict]:
    """Deterministic 5-minute unavailable windows at 06:00/12:00/18:00 UTC.

    The stress is fail-closed: decisions inside simulated reconnect gaps are removed,
    never forward-filled with stale features.
    """
    gap_starts = (360, 720, 1080)
    out = []
    for row in samples:
        minute_of_day = (row["decision_ts_us"] // 60_000_000) % 1440
        unavailable = any(start <= minute_of_day < start + 5 for start in gap_starts)
        if not unavailable:
            out.append(row)
    return out


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    primary_samples: list[dict] = []
    variants: dict[str, list[dict]] = {
        "clock_minus_1m": [],
        "clock_plus_1m": [],
        "exchange_timestamp_liquidations": [],
        "missing_liquidations_10pct": [],
        "reconnect_fail_closed": [],
        "one_liquidation_source_removed": [],
    }
    provenance = []
    excluded = defaultdict(int)
    date_stats = []

    with tempfile.TemporaryDirectory(prefix="aud066-r1-") as td_raw:
        td = Path(td_raw)
        for date in DATES:
            files = {}
            manifests = []
            failed = False
            for dtype in DATASETS:
                path = td / f"{date}-{dtype}.csv.gz"
                rec = download_bounded(dataset_url(dtype, date), path)
                rec.update({"date": date, "data_type": dtype})
                manifests.append(rec)
                if rec["status"] != "OK":
                    failed = True
                else:
                    files[dtype] = path
            provenance.extend(manifests)
            if failed:
                excluded["DATASET_DOWNLOAD_OR_AVAILABILITY_FAILURE"] += 1
                date_stats.append({"date": date, "status": "EXCLUDED_SOURCE_FAILURE", "manifests": manifests})
                continue

            liqs = []
            bad = 0
            liq_seen = 0
            for row in csv_rows(files["liquidations"]):
                if str(row.get("symbol") or "").upper() != "BTCUSDT":
                    continue
                liq_seen += 1
                normalized = normalize_liquidation(row)
                if normalized is None:
                    bad += 1
                else:
                    liqs.append(normalized)
            liqs.sort(key=lambda x: x["known_at_us"])

            counts = {"trades": 0, "quotes": 0, "derivative_ticker": 0}
            mins = build_minute_market(
                filtered_rows(files["trades"], counts, "trades"),
                filtered_rows(files["quotes"], counts, "quotes"),
                filtered_rows(files["derivative_ticker"], counts, "derivative_ticker"),
            )
            base, exc = make_samples(date, mins, liqs)
            base = decorate_context(base, mins)
            primary_samples.extend(base)
            for key, value in exc.items():
                excluded[key] += value
            excluded["LIQ_ROWS_BAD_TIMESTAMP_OR_SCHEMA"] += bad

            variants["clock_minus_1m"].extend(offset_samples(date, mins, liqs, -1))
            variants["clock_plus_1m"].extend(offset_samples(date, mins, liqs, +1))

            exchange_clock = [{**x, "known_at_us": x["exchange_ts_us"]} for x in liqs]
            xrows, _ = make_samples(date, mins, exchange_clock)
            variants["exchange_timestamp_liquidations"].extend(decorate_context(xrows, mins))

            missing = [x for idx, x in enumerate(liqs) if (idx + 1) % 10 != 0]
            mrows, _ = make_samples(date, mins, missing)
            variants["missing_liquidations_10pct"].extend(decorate_context(mrows, mins))

            variants["reconnect_fail_closed"].extend(reconnect_fail_closed(base))

            zero_rows, _ = make_samples(date, mins, [])
            variants["one_liquidation_source_removed"].extend(decorate_context(zero_rows, mins))

            date_stats.append({
                "date": date,
                "status": "USED",
                "liquidations_seen": liq_seen,
                "liquidations_accepted": len(liqs),
                "trades": counts["trades"],
                "quotes": counts["quotes"],
                "ticker_rows": counts["derivative_ticker"],
                "samples": len(base),
                "clock_minus_1m_samples": len([r for r in variants["clock_minus_1m"] if r["day"] == date]),
                "clock_plus_1m_samples": len([r for r in variants["clock_plus_1m"] if r["day"] == date]),
                "excluded": exc,
            })

    proxy_result = walk_forward_extended(primary_samples)
    proxy_result["proxy_baseline_only"] = True
    proxy_result["baseline_parity"] = BASELINE_PARITY
    proxy_result["proxy_realized_value_before_r1_correction"] = proxy_result.get("realized_value")
    inference = terminal_inference(proxy_result, BASELINE_PARITY)
    proxy_result["r1_inference_contract"] = inference
    proxy_result["realized_value"] = inference["REALIZED_LIQUIDATION_VALUE"]
    proxy_result["net_new_value"] = inference["NET_NEW_VALUE"]

    robustness = evaluate_robustness(primary_samples, variants, proxy_result)
    robustness["baseline_parity"] = BASELINE_PARITY
    robustness["authority"] = "PROXY_ROBUSTNESS_ONLY_NOT_CURRENT_SENEX_INCREMENTAL_VALUE"
    robustness["variant_sample_counts"] = {name: len(rows) for name, rows in variants.items()}

    manifest = {
        "order": "AUD-066-R1",
        "parent_order": "AUD-066",
        "base_sha": BASE_SHA,
        "base_tree": BASE_TREE,
        "source": "Tardis normalized first-of-month sample datasets exported from exchange real-time feeds",
        "zero_cost": True,
        "api_key_required": False,
        "dates_requested": DATES,
        "date_stats": date_stats,
        "sample_count": len(primary_samples),
        "excluded": dict(excluded),
        "provenance": provenance,
        "provenance_hash": canonical_hash([{k: v for k, v in r.items() if k != "error"} for r in provenance]),
        "point_in_time_clock": "local_timestamp(receipt time)",
        "label_rule": "BTC price at t+5m strictly after decision; never used in feature construction",
        "baseline_parity": BASELINE_PARITY,
        "baseline_parity_missing_inputs": PARITY_MISSING_INPUTS,
        "no_value_equivalence_criterion": "NOT_PREDECLARED_IN_PARENT_ORDER",
        "cost_usd": 0,
    }

    (ROOT / "data-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (ROOT / "oos-results.json").write_text(json.dumps(proxy_result, indent=2, sort_keys=True) + "\n")
    (ROOT / "robustness-matrix.json").write_text(json.dumps(robustness, indent=2, sort_keys=True) + "\n")

    lines = [
        "arm,status,test_blocks,mean_brier,mean_log_loss,mean_ece_10,calibration_intercept,calibration_slope,mean_accuracy,mean_balanced_accuracy,delta_brier_vs_A,delta_logloss_vs_A"
    ]
    if proxy_result.get("status") == "COMPLETE":
        summary = proxy_result["summary"]
        base = summary["A"]
        for arm in ("A", "B", "C"):
            x = summary[arm]
            lines.append(",".join(str(v) for v in [
                arm,
                "PROXY_ONLY_NOT_SENEX_PARITY",
                len(proxy_result["blocks"]),
                x["brier"], x["log_loss"], x["ece_10"], x["calibration_intercept"], x["calibration_slope"],
                x["accuracy"], x["balanced_accuracy"], x["brier"] - base["brier"], x["log_loss"] - base["log_loss"],
            ]))
    else:
        for arm in ("A", "B", "C"):
            lines.append(f"{arm},INCONCLUSIVE,0,,,,,,,,,")
    lines.extend([
        "D,NOT_TESTABLE_AT_ZERO_COST,0,,,,,,,,,",
        "E,VALIDATION_SELECTED_PER_BLOCK_PROXY_ONLY,,,,,,,,,,",
    ])
    (ROOT / "ablation-results.csv").write_text("\n".join(lines) + "\n")

    print("AUD066_R1_SAMPLE_COUNT=" + str(len(primary_samples)))
    print("BASELINE_PARITY=" + BASELINE_PARITY)
    print("REALIZED_LIQUIDATION_VALUE=" + inference["REALIZED_LIQUIDATION_VALUE"])
    print("NET_NEW_VALUE=" + inference["NET_NEW_VALUE"])
    print("ROBUSTNESS_MATRIX=" + robustness["status"])
    print("AUD066_R1_MANIFEST_HASH=" + canonical_hash(manifest))


if __name__ == "__main__":
    main()
