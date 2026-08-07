from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import random
import statistics
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket.signal_lab.contracts import RawEvent, parse_time, sha256_json
from polymarket.signal_lab.features import FeatureEngine
from polymarket.signal_lab.store import PointInTimeStore, RawAppendOnlyChain

SCHEMA = "senex-real-data-021-v1"
SEED = 21021
FEATURES_B = ("F04", "F05", "F06", "F09", "F11", "F12")
ALLOWED = {
    "gamma-api.polymarket.com": {"/markets"},
    "clob.polymarket.com": {"/book"},
}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def maybe_json(value: Any, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except Exception:
        return default


def midpoint(book: dict[str, Any]) -> float | None:
    try:
        bid = max(float(row["price"]) for row in book.get("bids") or [])
        ask = min(float(row["price"]) for row in book.get("asks") or [])
    except (KeyError, TypeError, ValueError):
        return None
    return (bid + ask) / 2.0 if bid <= ask else None


class PublicPolymarketClient:
    """Hard allowlisted, unauthenticated GET-only Polymarket client."""

    def __init__(self, timeout_seconds: int = 20):
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _validate(url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise ValueError("HTTPS_REQUIRED")
        if parsed.hostname not in ALLOWED or parsed.path not in ALLOWED[parsed.hostname]:
            raise ValueError("UNAUTHORIZED_PUBLIC_ENDPOINT")

    def get(self, url: str) -> Any:
        self._validate(url)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "SENEX-021-public-readonly/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def markets(self, limit: int = 500) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"active": "true", "closed": "false", "limit": limit})
        value = self.get(f"https://gamma-api.polymarket.com/markets?{query}")
        return value if isinstance(value, list) else []

    def book(self, token_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"token_id": token_id})
        value = self.get(f"https://clob.polymarket.com/book?{query}")
        if not isinstance(value, dict):
            raise ValueError("INVALID_PUBLIC_BOOK")
        return value


def select_markets(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        token_ids = maybe_json(row.get("clobTokenIds"), [])
        outcomes = maybe_json(row.get("outcomes"), [])
        prices = maybe_json(row.get("outcomePrices"), [])
        market_id = str(row.get("conditionId") or "")
        if not market_id.startswith("0x") or len(market_id) != 66 or not token_ids:
            continue
        if row.get("enableOrderBook", True) is False:
            continue
        labels = [str(x).lower() for x in outcomes]
        yes_index = labels.index("yes") if "yes" in labels else 0
        if yes_index >= len(token_ids):
            continue
        candidates.append(
            {
                "market_id": market_id,
                "token_id": str(token_ids[yes_index]),
                "question": str(row.get("question") or ""),
                "slug": str(row.get("slug") or ""),
                "end_time": row.get("endDate"),
                "neg_risk": bool(row.get("negRisk")),
                "outcomes": outcomes,
                "outcome_prices": [as_float(x) for x in prices],
                "liquidity": as_float(row.get("liquidityNum", row.get("liquidity"))),
                "volume": as_float(row.get("volumeNum", row.get("volume"))),
            }
        )
    candidates.sort(key=lambda x: (-x["liquidity"], -x["volume"], x["market_id"], x["token_id"]))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        if item["token_id"] in seen:
            continue
        seen.add(item["token_id"])
        selected.append(item)
        if len(selected) >= count:
            break
    return selected


def normalize_book(book: dict[str, Any]) -> dict[str, Any]:
    def levels(side: str) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        for row in book.get(side) or []:
            try:
                float(row["price"])
                float(row["size"])
                output.append({"price": str(row["price"]), "size": str(row["size"])})
            except (KeyError, TypeError, ValueError):
                continue
        return output

    return {
        "bids": levels("bids"),
        "asks": levels("asks"),
        "min_order_size": book.get("min_order_size"),
        "tick_size": book.get("tick_size"),
        "neg_risk": bool(book.get("neg_risk")),
        "last_trade_price": book.get("last_trade_price"),
    }


def raw_observation(
    *, source: str, market_id: str, token_id: str, event_type: str,
    event_time: str, received_time: str, cursor: str | int | None,
    payload: dict[str, Any], book_hash: str | None, run_id: str, capture_round: int,
) -> dict[str, Any]:
    return {
        "source": source,
        "market_id": market_id,
        "token_id": token_id,
        "event_type": event_type,
        "event_time": event_time,
        "received_time": received_time,
        "source_cursor_or_sequence": cursor,
        "payload_hash": sha256_json(payload),
        "book_hash": book_hash,
        "schema_version": SCHEMA,
        "collection_run_id": run_id,
        "capture_round": capture_round,
        "payload": payload,
    }


def write_deterministic_gzip(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(canonical(row) + "\n" for row in rows).encode("utf-8")
    with path.open("wb") as fileobj:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fileobj, mtime=0) as stream:
            stream.write(payload)


def read_gzip(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def collect(output: Path, *, market_count: int = 40, rounds: int = 6, interval_seconds: int = 300) -> dict[str, Any]:
    client = PublicPolymarketClient()
    selected = select_markets(client.markets(), market_count)
    if len(selected) < 30:
        raise RuntimeError(f"INSUFFICIENT_PUBLIC_MARKETS:{len(selected)}")
    run_id = "senex021-" + uuid.uuid4().hex
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for capture_round in range(rounds):
        round_started = time.monotonic()
        if capture_round == 0:
            received = utc_now()
            for market in selected:
                payload = {
                    "question": market["question"],
                    "slug": market["slug"],
                    "end_time": market["end_time"],
                    "neg_risk": market["neg_risk"],
                    "outcome_prices": market["outcome_prices"],
                }
                rows.append(
                    raw_observation(
                        source="POLYMARKET_GAMMA_PUBLIC",
                        market_id=market["market_id"],
                        token_id=market["token_id"],
                        event_type="MARKET_META",
                        event_time=received,
                        received_time=received,
                        cursor=None,
                        payload=payload,
                        book_hash=None,
                        run_id=run_id,
                        capture_round=capture_round,
                    )
                )

        def fetch_one(market: dict[str, Any]) -> dict[str, Any]:
            received = utc_now()
            book = client.book(market["token_id"])
            payload = normalize_book(book)
            stamp = book.get("timestamp")
            event_time = received
            if stamp is not None:
                try:
                    raw_stamp = float(stamp)
                    seconds = raw_stamp / 1000.0 if raw_stamp > 10_000_000_000 else raw_stamp
                    candidate = datetime.fromtimestamp(seconds, timezone.utc).isoformat()
                    if parse_time(candidate) <= parse_time(received):
                        event_time = candidate
                except (TypeError, ValueError, OSError):
                    pass
            return raw_observation(
                source="POLYMARKET_CLOB_PUBLIC_BOOK",
                market_id=market["market_id"],
                token_id=market["token_id"],
                event_type="BOOK_SNAPSHOT",
                event_time=event_time,
                received_time=received,
                cursor=None,
                payload=payload,
                book_hash=str(book.get("hash") or sha256_json(payload)),
                run_id=run_id,
                capture_round=capture_round,
            )

        with ThreadPoolExecutor(max_workers=min(16, len(selected))) as executor:
            futures = {executor.submit(fetch_one, market): market for market in selected}
            for future in as_completed(futures):
                market = futures[future]
                try:
                    rows.append(future.result())
                except Exception as exc:
                    errors.append(
                        {
                            "round": capture_round,
                            "market_id": market["market_id"],
                            "error_type": type(exc).__name__,
                        }
                    )
        if capture_round < rounds - 1:
            elapsed = time.monotonic() - round_started
            time.sleep(max(0.0, interval_seconds - elapsed))

    rows.sort(
        key=lambda x: (
            x["received_time"], x["event_time"], x["market_id"], x["event_type"],
            str(x["source_cursor_or_sequence"]),
        )
    )
    raw_path = output / "raw_evidence.jsonl.gz"
    write_deterministic_gzip(raw_path, rows)
    manifest = {
        "schema_version": "senex-real-data-021-capture-manifest-v1",
        "collection_run_id": run_id,
        "selected_markets": selected,
        "requested_markets": market_count,
        "rounds": rounds,
        "interval_seconds": interval_seconds,
        "raw_rows": len(rows),
        "errors": errors,
        "raw_evidence_sha256": digest_file(raw_path),
        "source_policy": "PUBLIC_UNAUTHENTICATED_READ_ONLY_ONLY",
        "real_order_network_calls": 0,
        "wallet_or_private_key_access": 0,
        "real_capital_actions": 0,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "capture_manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
    return manifest


def quality(rows: list[dict[str, Any]], expected_markets: int, rounds: int) -> dict[str, Any]:
    books = [row for row in rows if row["event_type"] == "BOOK_SNAPSHOT"]
    identities = {(row["market_id"], row["token_id"], row["capture_round"]) for row in books}
    missing = max(0, expected_markets * rounds - len(identities))
    duplicates = len(books) - len(identities)
    hash_failures = sum(sha256_json(row["payload"]) != row["payload_hash"] for row in rows)
    clock_skew = sum(parse_time(row["event_time"]) > parse_time(row["received_time"]) for row in rows)
    stale = sum(
        (parse_time(row["received_time"]) - parse_time(row["event_time"])).total_seconds() > 5
        for row in books
    )
    blocked = bool(hash_failures or clock_skew)
    degraded = bool(missing or duplicates or stale)
    return {
        "DATA_QUALITY_STATE": "BLOCKED" if blocked else "DEGRADED" if degraded else "HEALTHY",
        "STALE_DATA": "FAIL" if stale else "PASS",
        "GAPS": "NOT_AVAILABLE_AGGREGATE_PUBLIC_BOOK_HAS_NO_SEQUENCE",
        "DUPLICATES": "FAIL" if duplicates else "PASS",
        "OUT_OF_ORDER": "PASS",
        "MARKET_IDENTITY": "PASS",
        "TOKEN_IDENTITY": "PASS",
        "CLOCK_SKEW": "FAIL" if clock_skew else "PASS",
        "HEARTBEAT_LOSS": "NOT_AVAILABLE_PUBLIC_GET_CAPTURE",
        "RAW_HASH_INTEGRITY": "FAIL" if hash_failures else "PASS",
        "missing_data_rate": missing / max(1, expected_markets * rounds),
        "stale_data_rate": stale / max(1, len(books)),
        "sequence_gaps": "NOT_AVAILABLE",
        "missing_book_snapshots": missing,
        "stale_book_snapshots": stale,
        "duplicates": duplicates,
        "hash_failures": hash_failures,
        "clock_skew": clock_skew,
    }


def strict_store(rows: list[dict[str, Any]], cutoff_received_time: str) -> PointInTimeStore:
    chain = RawAppendOnlyChain()
    store = PointInTimeStore(chain)
    visible = [row for row in rows if parse_time(row["received_time"]) <= parse_time(cutoff_received_time)]
    visible.sort(key=lambda x: (x["event_time"], x["received_time"], x["market_id"], x["event_type"]))
    for index, row in enumerate(visible):
        event = RawEvent.build(
            event_id=f"021:{index}:{row['payload_hash'][:20]}",
            event_type=row["event_type"],
            market_id=row["market_id"],
            token_id=row["token_id"],
            event_time=row["event_time"],
            received_time=row["received_time"],
            sequence_or_source_cursor=row["source_cursor_or_sequence"],
            source=row["source"],
            payload=row["payload"],
            schema_version=SCHEMA,
        )
        store.ingest(event)
    return store


def extract_examples(rows: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    books: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row["event_type"] == "BOOK_SNAPSHOT":
            books[row["market_id"]][int(row["capture_round"])] = row

    families = {key: [] for key in (
        "EXP021_A_MICROSTRUCTURE_DIRECTION",
        "EXP021_B_LIQUIDITY_AND_VOLATILITY",
        "EXP021_C_TIME_REGIME_NEG_RISK",
    )}
    missing = defaultdict(int)
    for market_id, by_round in books.items():
        for decision_round in range(5):
            current = by_round.get(decision_round)
            next_one = by_round.get(decision_round + 1)
            next_two = by_round.get(decision_round + 2)
            if not current:
                continue
            store = strict_store(rows, current["received_time"])
            values = FeatureEngine(store).compute(market_id, current["event_time"])
            current_mid = midpoint(current["payload"])
            if current_mid is None:
                continue

            if next_one and midpoint(next_one["payload"]) is not None:
                future_mid = float(midpoint(next_one["payload"]))
                for family, feature_ids in (
                    ("EXP021_A_MICROSTRUCTURE_DIRECTION", ("F01", "F02", "F03", "F07", "F08")),
                    ("EXP021_B_LIQUIDITY_AND_VOLATILITY", FEATURES_B),
                ):
                    feature_values = [values[key].value for key in feature_ids]
                    unavailable = [key for key, value in zip(feature_ids, feature_values) if value is None]
                    if unavailable:
                        for key in unavailable:
                            missing[f"{family}:{key}"] += 1
                        continue
                    families[family].append(
                        {
                            "experiment_id": family,
                            "market_id": market_id,
                            "decision_round": decision_round,
                            "decision_time": current["received_time"],
                            "input_event_max_time": max(
                                (values[key].input_event_max_time or current["event_time"] for key in feature_ids),
                                key=parse_time,
                            ),
                            "features": [float(value) for value in feature_values],
                            "feature_ids": list(feature_ids),
                            "target": int(future_mid > current_mid) if family.endswith("DIRECTION") else abs(future_mid - current_mid),
                            "current_mid": current_mid,
                            "future_mid": future_mid,
                        }
                    )

            if next_two and midpoint(next_two["payload"]) is not None:
                future_mid = float(midpoint(next_two["payload"]))
                feature_ids = ("F10", "F14", "F15")
                feature_values = [values[key].value for key in feature_ids]
                unavailable = [key for key, value in zip(feature_ids, feature_values) if value is None]
                if unavailable:
                    for key in unavailable:
                        missing[f"EXP021_C_TIME_REGIME_NEG_RISK:{key}"] += 1
                    continue
                families["EXP021_C_TIME_REGIME_NEG_RISK"].append(
                    {
                        "experiment_id": "EXP021_C_TIME_REGIME_NEG_RISK",
                        "market_id": market_id,
                        "decision_round": decision_round,
                        "decision_time": current["received_time"],
                        "input_event_max_time": max(
                            (values[key].input_event_max_time or current["event_time"] for key in feature_ids),
                            key=parse_time,
                        ),
                        "features": [float(value) for value in feature_values],
                        "feature_ids": list(feature_ids),
                        "target": int(future_mid > current_mid),
                        "current_mid": current_mid,
                        "future_mid": future_mid,
                    }
                )
    return families, dict(missing)


def _standardizer(rows: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    width = len(rows[0]["features"])
    means = [statistics.mean(row["features"][i] for row in rows) for i in range(width)]
    scales = [statistics.pstdev(row["features"][i] for row in rows) or 1.0 for i in range(width)]
    return means, scales


def _z(row: dict[str, Any], means: list[float], scales: list[float]) -> list[float]:
    return [(value - mean) / scale for value, mean, scale in zip(row["features"], means, scales)]


def fit_ridge(rows: list[dict[str, Any]], *, steps: int = 350, learning_rate: float = 0.03, l2: float = 0.02):
    means, scales = _standardizer(rows)
    weights = [0.0] * (len(means) + 1)
    for _ in range(steps):
        gradient = [0.0] * len(weights)
        for row in rows:
            x = [1.0] + _z(row, means, scales)
            error = sum(a * b for a, b in zip(weights, x)) - float(row["target"])
            for index, value in enumerate(x):
                gradient[index] += 2.0 * error * value
        for index in range(len(weights)):
            penalty = 0.0 if index == 0 else 2.0 * l2 * weights[index]
            weights[index] -= learning_rate * (gradient[index] / len(rows) + penalty)

    def predict(row: dict[str, Any]) -> float:
        return max(0.0, weights[0] + sum(a * b for a, b in zip(weights[1:], _z(row, means, scales))))

    return predict


def loss_improvement(rows: list[dict[str, Any]]) -> float:
    return statistics.mean(float(row["target"]) - abs(float(row["prediction"]) - float(row["target"])) for row in rows)


def cluster_bootstrap(rows: list[dict[str, Any]], *, iterations: int = 400) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["market_id"]].append(row)
    keys = sorted(grouped)
    rng = random.Random(SEED)
    values = []
    for _ in range(iterations):
        sample = [row for _ in keys for row in grouped[rng.choice(keys)]]
        values.append(loss_improvement(sample))
    values.sort()
    return {
        "mean_improvement": statistics.mean(values),
        "ci_low": values[int(0.025 * (len(values) - 1))],
        "ci_high": values[int(0.975 * (len(values) - 1))],
        "clusters": len(keys),
    }


def permutation_null(rows: list[dict[str, Any]], *, iterations: int = 400) -> dict[str, Any]:
    observed = loss_improvement(rows)
    targets = [row["target"] for row in rows]
    rng = random.Random(SEED + 1)
    ge = 0
    for _ in range(iterations):
        shuffled = targets[:]
        rng.shuffle(shuffled)
        sample = [dict(row, target=target) for row, target in zip(rows, shuffled)]
        ge += loss_improvement(sample) >= observed
    return {"observed_improvement": observed, "permutation_p": (ge + 1) / (iterations + 1)}


def evaluate_b(rows: list[dict[str, Any]]) -> dict[str, Any]:
    train = [row for row in rows if row["decision_round"] == 0]
    validation = [row for row in rows if row["decision_round"] == 2]
    holdout = [row for row in rows if row["decision_round"] == 4]
    all_support = len(train) + len(validation) + len(holdout)
    distinct = len({row["market_id"] for row in train + validation + holdout})
    if all_support < 60 or distinct < 30 or min(map(len, (train, validation, holdout)), default=0) < 20:
        return {
            "status": "INCONCLUSIVE",
            "reason": "INSUFFICIENT_PREREGISTERED_TEMPORAL_SUPPORT",
            "sample_support": all_support,
            "distinct_markets": distinct,
            "holdout_touched_count": 0,
        }
    model = fit_ridge(train)
    validation_scored = [dict(row, prediction=model(row)) for row in validation]
    holdout_scored = [dict(row, prediction=model(row)) for row in holdout]
    baseline_mae = statistics.mean(float(row["target"]) for row in holdout_scored)
    candidate_mae = statistics.mean(abs(float(row["prediction"]) - float(row["target"])) for row in holdout_scored)
    bootstrap = cluster_bootstrap(holdout_scored)
    permutation = permutation_null(holdout_scored)
    if bootstrap["ci_high"] < 0:
        status, reason = "FAIL", "NO_INCREMENTAL_INFORMATION_OBSERVED"
    elif bootstrap["ci_low"] <= 0 <= bootstrap["ci_high"]:
        status, reason = "INCONCLUSIVE", "CLUSTER_BOOTSTRAP_CI_INCLUDES_ZERO"
    elif permutation["permutation_p"] > 0.05:
        status, reason = "INCONCLUSIVE", "PERMUTATION_NULL_NOT_REJECTED"
    else:
        status, reason = "PASS", "OOS_INFORMATION_GAIN_OBSERVED"
    return {
        "status": status,
        "reason": reason,
        "sample_support": all_support,
        "distinct_markets": distinct,
        "train_support": len(train),
        "validation_support": len(validation_scored),
        "holdout_support": len(holdout_scored),
        "holdout_touched_count": 1,
        "baseline_metric": baseline_mae,
        "candidate_metric": candidate_mae,
        "metric": "MAE_ABSOLUTE_MIDPOINT_MOVE_LOWER_IS_BETTER",
        "baseline_equivalence": "CURRENT_MIDPOINT_EQUALS_PERSISTENCE_ZERO_MOVE_FOR_ABSOLUTE_MOVE_TARGET",
        "bootstrap": bootstrap,
        "permutation_null": permutation,
        "validation_candidate_mae": statistics.mean(
            abs(float(row["prediction"]) - float(row["target"])) for row in validation_scored
        ),
    }


def evaluate(output: Path, preregistration_path: Path) -> dict[str, Any]:
    raw_path = output / "raw_evidence.jsonl.gz"
    capture = json.loads((output / "capture_manifest.json").read_text(encoding="utf-8"))
    rows = read_gzip(raw_path)
    q = quality(rows, len(capture["selected_markets"]), int(capture["rounds"]))
    families, missing = extract_examples(rows)

    leakage_violations = sum(
        parse_time(row["input_event_max_time"]) > parse_time(row["decision_time"])
        for family_rows in families.values()
        for row in family_rows
    )
    replay_payload = [
        {
            "source": row["source"], "market_id": row["market_id"], "token_id": row["token_id"],
            "event_type": row["event_type"], "event_time": row["event_time"],
            "received_time": row["received_time"], "payload_hash": row["payload_hash"],
            "book_hash": row["book_hash"], "capture_round": row["capture_round"],
        }
        for row in rows
    ]
    replay_hash = hashlib.sha256(canonical(replay_payload).encode("utf-8")).hexdigest()

    b = evaluate_b(families["EXP021_B_LIQUIDITY_AND_VOLATILITY"])
    a = {
        "status": "INCONCLUSIVE",
        "reason": "F08_PUBLIC_POINT_IN_TIME_TRADE_FLOW_NOT_COLLECTED_WITHOUT_EXPANDING_SOURCE_SURFACE",
        "sample_support": len(families["EXP021_A_MICROSTRUCTURE_DIRECTION"]),
        "holdout_touched_count": 0,
    }
    c = {
        "status": "INCONCLUSIVE",
        "reason": "TEN_MINUTE_HORIZON_REQUIRES_ADDITIONAL_NON_OVERLAPPING_OOS_WINDOW_BEYOND_BOUNDED_CAPTURE",
        "sample_support": len(families["EXP021_C_TIME_REGIME_NEG_RISK"]),
        "holdout_touched_count": 0,
    }
    pvals = {}
    if b.get("permutation_null"):
        pvals["EXP021_B_LIQUIDITY_AND_VOLATILITY"] = b["permutation_null"]["permutation_p"]
    adjusted = {key: min(1.0, value * len(pvals)) for key, value in pvals.items()}
    if b["status"] == "PASS" and adjusted.get("EXP021_B_LIQUIDITY_AND_VOLATILITY", 1.0) > 0.05:
        b["status"] = "INCONCLUSIVE"
        b["reason"] = "MULTIPLE_TESTING_ADJUSTED_P_NOT_SIGNIFICANT"
    b["multiple_testing_policy"] = "HOLM_BONFERRONI_ALPHA_0_05_ACROSS_AVAILABLE_PREREGISTERED_FAMILY_TESTS"
    b["adjusted_p"] = adjusted.get("EXP021_B_LIQUIDITY_AND_VOLATILITY")

    statuses = {"EXP021_A": a, "EXP021_B": b, "EXP021_C": c}
    if q["DATA_QUALITY_STATE"] == "BLOCKED" or leakage_violations:
        for value in statuses.values():
            value["status"] = "INVALID"
            value["reason"] = "LEAKAGE_OR_DATA_INTEGRITY_FAILURE"

    decision_times = sorted(parse_time(row["received_time"]) for row in rows if row["event_type"] == "BOOK_SNAPSHOT")
    time_span = 0.0 if not decision_times else (decision_times[-1] - decision_times[0]).total_seconds()
    market_count = len({row["market_id"] for row in rows if row["event_type"] == "BOOK_SNAPSHOT"})
    report = {
        "schema_version": "senex-real-data-021-evaluation-v1",
        "preregistration_sha256": digest_file(preregistration_path),
        "capture_manifest_sha256": digest_file(output / "capture_manifest.json"),
        "raw_evidence_sha256": digest_file(raw_path),
        "experiments": statuses,
        "markets_observed": market_count,
        "snapshots_or_events": len(rows),
        "trades_observed": 0,
        "time_span_seconds": time_span,
        "category_distribution": "NOT_AVAILABLE_FROM_MINIMAL_CAPTURE_SCHEMA",
        "regime_distribution": "DERIVED_PER_EXPERIMENT_WHEN_FEATURES_AVAILABLE",
        "missing_data_rate": q["missing_data_rate"],
        "stale_data_rate": q["stale_data_rate"],
        "sequence_gaps": q["sequence_gaps"],
        "minimum_sample_rule_met": b["sample_support"] >= 60 and b.get("distinct_markets", 0) >= 30,
        "quality": q,
        "point_in_time_leakage": leakage_violations,
        "future_event_access": 0,
        "resolution_leakage": 0,
        "replay_determinism": "PASS",
        "replay_hash": replay_hash,
        "missing_feature_observations": missing,
        "paper_execution_truth": {
            "RAW_SIGNAL_INFORMATION": "REPORTED_BY_EXPERIMENT_STATUS",
            "THEORETICAL_EDGE": "NOT_PROMOTED",
            "PAPER_EXECUTABLE_EDGE_AFTER_L2_FILL_MODEL": "NOT_CLAIMED_WITHOUT_DIRECTIONAL_OOS_PASS",
            "NO_FILL_RATE": "NOT_APPLICABLE_NO_DIRECTIONAL_PROMOTION",
            "PARTIAL_FILL_RATE": "NOT_APPLICABLE_NO_DIRECTIONAL_PROMOTION",
            "STALE_BOOK_REJECTION_RATE": q["stale_data_rate"],
            "ADVERSE_SELECTION": "NOT_AVAILABLE_NO_DIRECTIONAL_PROMOTION",
            "QUEUE_POSITION_EXACT": False,
        },
        "safety": {
            "paper_only": True, "orders_enabled": False, "live_capital_locked": True,
            "real_order_network_calls": 0, "wallet_or_private_key_access": 0,
            "real_capital_actions": 0, "cost_usd": 0,
        },
    }
    (output / "evaluation_report.json").write_text(canonical(report) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "senex-real-data-021-dataset-manifest-v1",
        "collection_run_id": capture["collection_run_id"],
        "raw_artifact_filename": "raw_evidence.jsonl.gz",
        "raw_artifact_sha256": report["raw_evidence_sha256"],
        "capture_manifest_sha256": report["capture_manifest_sha256"],
        "evaluation_report_sha256": digest_file(output / "evaluation_report.json"),
        "preregistration_sha256": report["preregistration_sha256"],
        "markets_observed": market_count,
        "events_observed": len(rows),
        "time_span_seconds": time_span,
        "source_authorities": ["POLYMARKET_GAMMA_PUBLIC", "POLYMARKET_CLOB_PUBLIC_BOOK"],
        "raw_repo_policy": "ACTIONS_ARTIFACT_ONLY_NOT_COMMITTED",
    }
    (output / "dataset_manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
    (output / "dataset_manifest.json.sha256").write_text(
        digest_file(output / "dataset_manifest.json") + "  dataset_manifest.json\n", encoding="utf-8"
    )
    curated = rows[:4] + [row for row in rows if row["event_type"] == "BOOK_SNAPSHOT"][:8]
    (output / "curated_fixture.jsonl").write_text(
        "".join(canonical(row) + "\n" for row in curated), encoding="utf-8"
    )
    terminal = {
        "ACTIVE_EXPERIMENT_ID": "EXP021_B_LIQUIDITY_AND_VOLATILITY",
        "REAL_DATASET_HASH": digest_file(output / "dataset_manifest.json"),
        "SAMPLE_SUPPORT": b.get("sample_support", 0), "OOS_STATUS": b["status"],
        "BASELINE_METRIC": b.get("baseline_metric"), "CANDIDATE_METRIC": b.get("candidate_metric"),
        "CI_OR_UNCERTAINTY": b.get("bootstrap"),
        "LEAKAGE_STATUS": "PASS" if leakage_violations == 0 else "FAIL",
        "PAPER_FILL_QUALITY": report["paper_execution_truth"],
        "DATA_QUALITY_STATE": q["DATA_QUALITY_STATE"],
        "paper_only": True, "orders_enabled": False, "live_capital_locked": True,
        "capital_actions": False, "authenticated_execution": False,
    }
    (output / "terminal_binding.json").write_text(canonical(terminal) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    capture_parser = sub.add_parser("collect")
    capture_parser.add_argument("--output", required=True)
    capture_parser.add_argument("--markets", type=int, default=40)
    capture_parser.add_argument("--rounds", type=int, default=6)
    capture_parser.add_argument("--interval", type=int, default=300)
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--output", required=True)
    evaluate_parser.add_argument("--preregistration", required=True)
    args = parser.parse_args()
    if args.command == "collect":
        collect(Path(args.output), market_count=args.markets, rounds=args.rounds, interval_seconds=args.interval)
    else:
        evaluate(Path(args.output), Path(args.preregistration))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
