import hashlib
from pathlib import Path

import pytest

from polymarket.mega_research.real_data_021 import (
    PublicPolymarketClient,
    cluster_bootstrap,
    evaluate_b,
    loss_improvement,
    permutation_null,
    quality,
    raw_observation,
    read_gzip,
    strict_store,
    write_deterministic_gzip,
)
from polymarket.signal_lab.contracts import sha256_json


def test_public_client_is_https_get_allowlist_only():
    PublicPolymarketClient._validate("https://gamma-api.polymarket.com/markets?active=true")
    PublicPolymarketClient._validate("https://clob.polymarket.com/book?token_id=123")
    for url in (
        "http://gamma-api.polymarket.com/markets",
        "https://clob.polymarket.com/order",
        "https://example.com/book",
    ):
        with pytest.raises(ValueError):
            PublicPolymarketClient._validate(url)


def test_deterministic_gzip_and_hash(tmp_path: Path):
    rows = [{"z": 1, "a": "x"}, {"a": "y", "z": 2}]
    a, b = tmp_path / "a.gz", tmp_path / "b.gz"
    write_deterministic_gzip(a, rows)
    write_deterministic_gzip(b, rows)
    assert a.read_bytes() == b.read_bytes()
    assert hashlib.sha256(a.read_bytes()).hexdigest() == hashlib.sha256(b.read_bytes()).hexdigest()
    assert read_gzip(a) == rows


def _book_row(round_no: int, received: str, bid="0.40", ask="0.60"):
    payload = {"bids": [{"price": bid, "size": "10"}], "asks": [{"price": ask, "size": "12"}]}
    return raw_observation(
        source="POLYMARKET_CLOB_PUBLIC_BOOK",
        market_id="0x" + "1" * 64,
        token_id="123",
        event_type="BOOK_SNAPSHOT",
        event_time=received,
        received_time=received,
        cursor=None,
        payload=payload,
        book_hash=sha256_json(payload),
        run_id="run",
        capture_round=round_no,
    )


def test_received_time_cutoff_prevents_future_capture_visibility():
    rows = [
        _book_row(0, "2026-08-07T10:00:00+00:00"),
        _book_row(1, "2026-08-07T10:05:00+00:00", "0.45", "0.65"),
    ]
    store = strict_store(rows, "2026-08-07T10:00:00+00:00")
    visible = store.events_as_of("2026-08-07T10:10:00+00:00")
    assert len(visible) == 1
    assert visible[0].received_time == "2026-08-07T10:00:00+00:00"


def test_quality_fails_closed_on_raw_hash_corruption():
    row = _book_row(0, "2026-08-07T10:00:00+00:00")
    row["payload_hash"] = "0" * 64
    report = quality([row], 1, 1)
    assert report["DATA_QUALITY_STATE"] == "BLOCKED"
    assert report["RAW_HASH_INTEGRITY"] == "FAIL"


def _ridge_rows(markets=32):
    rows = []
    for m in range(markets):
        base = (m + 1) / 1000
        for round_no in (0, 2, 4):
            features = [base, base * 2, base * 3, base * 4, base * 5, base * 6]
            target = max(0.0, 0.001 + 0.4 * features[0] + 0.2 * features[1])
            rows.append(
                {
                    "market_id": f"m{m}",
                    "decision_round": round_no,
                    "features": features,
                    "target": target,
                }
            )
    return rows


def test_bounded_b_evaluation_touches_holdout_once_and_is_deterministic():
    rows = _ridge_rows()
    first = evaluate_b(rows)
    second = evaluate_b(rows)
    assert first == second
    assert first["holdout_touched_count"] == 1
    assert first["sample_support"] >= 60
    assert first["distinct_markets"] >= 30
    assert first["status"] in {"PASS", "FAIL", "INCONCLUSIVE"}


def test_insufficient_support_is_inconclusive_without_holdout_touch():
    report = evaluate_b(_ridge_rows(5))
    assert report["status"] == "INCONCLUSIVE"
    assert report["holdout_touched_count"] == 0


def test_statistical_helpers_are_deterministic():
    rows = []
    for i in range(30):
        target = 0.001 + i / 10000
        rows.append({"market_id": f"m{i}", "target": target, "prediction": target * 0.9})
    assert loss_improvement(rows) == loss_improvement(rows)
    assert cluster_bootstrap(rows, iterations=50) == cluster_bootstrap(rows, iterations=50)
    assert permutation_null(rows, iterations=50) == permutation_null(rows, iterations=50)
