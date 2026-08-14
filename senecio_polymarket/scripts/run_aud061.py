#!/usr/bin/env python3
"""Run AUD-061 research over immutable read-only exports."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.research.aud061_pipeline import canonical_hash, run_all
from oracle.exchange_connector import build_feature_observations


def post_fix_feature_fixtures() -> dict:
    candles = [[0, 100, 101, 99, 100, 10], [1, 100, 101, 99, 100, 10]]
    scenarios = {
        "okx_observed_zero_plus_oi_snapshot": build_feature_observations(
            exchange="okx", ohlcv=candles,
            orderbook={"bid_depth_usdt": 100, "ask_depth_usdt": 100},
            funding={"rate": 0.0},
            open_interest={"oi_value": 123.0, "oi_change_24h_pct": 0.0},
        ),
        "kraken_spot_fallback_unavailable": build_feature_observations(
            exchange="kraken", ohlcv=None, orderbook=None, funding=None,
            open_interest=None, fallback_used=True,
        ),
    }
    counts = {}
    for scenario in scenarios.values():
        for feature, observation in scenario.items():
            key = f"{feature}|{observation['status']}"
            counts[key] = counts.get(key, 0) + 1
    return {
        "status": "COMPLETE_DETERMINISTIC_CANDIDATE_FIXTURES",
        "counts": dict(sorted(counts.items())),
        "scenarios": scenarios,
        "candidate_not_deployed": True,
        "live_post_fix_observation": "NOT_APPLICABLE_NO_DEPLOY",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--output", required=True)
    parser.add_argument("--source", action="append", default=[])
    args = parser.parse_args()
    rows = []
    sources = []
    for filename in args.inputs:
        path = Path(filename)
        payload = json.loads(path.read_text(encoding="utf-8"))
        extracted = payload.get("predictions", payload) if isinstance(payload, dict) else payload
        if not isinstance(extracted, list):
            raise ValueError(f"{path}: expected list or predictions wrapper")
        rows.extend(extracted)
        sources.append({"path_label": path.name, "sha256": canonical_hash(payload), "rows": len(extracted)})
    # De-duplicate deterministic API exports by stable prediction ID.
    unique = {}
    for row in rows:
        unique[(str(row.get("symbol")), str(row.get("id")), str(row.get("ts")))] = row
    report = run_all(list(unique.values()))
    report["feature_availability"]["post_fix"] = post_fix_feature_fixtures()
    report["manifest"] = {
        "order": "AUD-061",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "source_endpoints": args.source,
        "read_only": True,
        "supabase_mutations": 0,
        "production_writeback": False,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "rows": report["input_rows"],
        "learning_status": report["learning_ab"]["status"],
        "learning_effect": report["learning_ab"]["learning_effect"],
        "edge_claim_supported": report["edge_claim_supported"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
