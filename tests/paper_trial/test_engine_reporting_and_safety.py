from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from paper.broker import PublicOrderBook, SimulatedBroker
from paper.engine import PaperEngine
from paper.portfolio import PaperPortfolio
from paper.report import REQUIRED_ARTIFACTS, TrialArtifactWriter, verify_artifact_bundle
from paper.risk import PaperRiskConfig, PaperRiskEngine
from paper.trial_runner import (
    BtcWindowDiscovery,
    PublicDataClient,
    PublicSourceError,
    TrialClock,
    TrialConfig,
    classify_book_failure,
    run_trial,
)

TS = "2026-08-04T12:00:00Z"
ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, epoch: float):
        self._epoch = float(epoch)
        self._monotonic = 0.0

    def monotonic(self) -> float:
        return self._monotonic

    def epoch(self) -> float:
        return self._epoch

    def sleep(self, seconds: float) -> None:
        self._monotonic += float(seconds)
        self._epoch += float(seconds)

    def now_utc(self) -> str:
        return datetime.fromtimestamp(self._epoch, timezone.utc).isoformat().replace("+00:00", "Z")

    def as_trial_clock(self) -> TrialClock:
        return TrialClock(
            monotonic=self.monotonic,
            epoch=self.epoch,
            sleep=self.sleep,
            now_utc=self.now_utc,
        )


class GammaOnlyClient:
    def __init__(self):
        self.requests = []
        self.evidence = []
        self.failures = 0
        self.book_calls: dict[str, int] = {}

    def close(self) -> None:
        return None

    def discover_active_btc_windows(self, *, now_epoch: int) -> BtcWindowDiscovery:
        slug = f"btc-updown-5m-{(now_epoch // 300) * 300}"
        return BtcWindowDiscovery(
            discovered_windows=(slug,),
            eligible_markets=(),
            expected_closed_no_book_windows=(),
            malformed_windows=(),
        )


class ValidBookClient(GammaOnlyClient):
    def discover_active_btc_windows(self, *, now_epoch: int) -> BtcWindowDiscovery:
        self._now_epoch = now_epoch
        slug = f"btc-updown-5m-{(now_epoch // 300) * 300}"
        market = {
            "id": slug,
            "conditionId": slug,
            "closed": False,
            "acceptingOrders": True,
            "active": True,
            "clobTokenIds": ["yes", "no"],
            "outcomes": ["UP", "DOWN"],
            "_window_slug": slug,
            "_window_epoch": (now_epoch // 300) * 300,
            "_window_end_epoch": ((now_epoch // 300) * 300) + 300,
        }
        return BtcWindowDiscovery(
            discovered_windows=(slug,),
            eligible_markets=(market,),
            expected_closed_no_book_windows=(),
            malformed_windows=(),
        )

    def market_info(self, condition_id: str):
        payload = {"condition_id": condition_id, "fd": {"r": 0, "e": 2, "to": True}, "itode": False}
        return payload, hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def book(self, token_id: str):
        count = self.book_calls.get(token_id, 0) + 1
        self.book_calls[token_id] = count
        timestamp_ms = int(self._now_epoch * 1000) + (500 if token_id == "no" and count > 1 else 0)
        payload = {
            "asset_id": token_id,
            "timestamp": str(timestamp_ms),
            "bids": [{"price": "0.45", "size": "20"}],
            "asks": [{"price": "0.56" if token_id == "no" and count > 1 else "0.46", "size": "20"}],
        }
        return payload, hashlib.sha256(f"{token_id}:{count}:{timestamp_ms}".encode()).hexdigest()


def fixture_engine():
    return PaperEngine(
        broker=SimulatedBroker(slippage_bps_floor=0),
        risk=PaperRiskEngine(PaperRiskConfig()),
        portfolio=PaperPortfolio(starting_equity_usd=10000),
    )


def books():
    return {
        token: PublicOrderBook.from_payload(
            market_id="m", token_id=token, timestamp_utc=TS,
            source_evidence_hash=hashlib.sha256(token.encode()).hexdigest(),
            fixture_timestamp_utc=TS,
            payload={"asset_id": token, "bids": [{"price": "0.45", "size": "10"}], "asks": [{"price": "0.46", "size": "10"}]},
        )
        for token in ("yes", "no")
    }


def executable_record():
    return {
        "market_id": "m", "condition_id": "m", "token_ids": ["yes", "no"],
        "shadow_execution": {"status": "SHADOW_EXECUTABLE", "net_edge": 0.08},
        "evidence_verified": True, "raw_chain_verified": True,
        "replay_verified": True, "regime_known": True,
    }


def test_simulated_execution_integration_records_two_equal_legs():
    engine = fixture_engine()
    result = engine.process_h011_record(
        record=executable_record(), books=books(), outcomes={"yes": "UP", "no": "DOWN"},
        timestamp_utc=TS, code_sha="a" * 40, config_sha="b" * 64,
        requested_shares=2, max_notional_per_leg_usd=50,
    )
    assert result.risk_decision and result.risk_decision.allowed
    assert len(result.fills) == 2
    assert result.fills[0].filled_shares == result.fills[1].filled_shares
    assert engine.portfolio.cash_usd < 10000


def test_no_trade_records_explicit_abstention():
    record = executable_record()
    record["shadow_execution"] = {"status": "REJECTED", "net_edge": -0.01}
    result = fixture_engine().process_h011_record(
        record=record, books=books(), outcomes={"yes": "UP", "no": "DOWN"},
        timestamp_utc=TS, code_sha="a" * 40, config_sha="b" * 64,
        requested_shares=2, max_notional_per_leg_usd=50,
    )
    assert result.fills == []
    assert "INSUFFICIENT_EDGE" in result.abstention_reasons


def test_fixture_trial_writes_and_hashes_all_artifacts(tmp_path: Path):
    result = run_trial(
        output_dir=tmp_path,
        config=TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=1),
        fixture=True,
    )
    assert result["summary"]["fills"] == 2
    assert result["summary"]["replay_result"] == "PASS"
    assert set(REQUIRED_ARTIFACTS).issubset(path.name for path in tmp_path.iterdir())
    assert verify_artifact_bundle(tmp_path)


def test_fixture_trial_artifacts_are_host_readable(tmp_path: Path):
    run_trial(
        output_dir=tmp_path,
        config=TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=1),
        fixture=True,
    )
    for name in (*REQUIRED_ARTIFACTS, "SHA256SUMS"):
        assert (tmp_path / name).stat().st_mode & 0o444 == 0o444, name


def test_active_window_selector_rejects_expired_settlement_lag():
    now_epoch = 1_785_885_000
    current_slug = f"btc-updown-5m-{(now_epoch // 300) * 300}"
    client = PublicDataClient()
    try:
        client.get_json = lambda url, params=None: [{
            "endDate": "2026-08-04T23:10:00Z",
            "markets": [{
                "id": "expired",
                "closed": False,
                "acceptingOrders": True,
                "endDate": "2026-08-04T23:10:00Z",
            }],
        }]
        assert client.active_btc_windows(now_epoch=now_epoch) == []

        client.get_json = lambda url, params=None: [{
            "endDate": "2026-08-04T23:20:00Z",
            "markets": [{
                "id": "live",
                "closed": False,
                "acceptingOrders": True,
                "endDate": "2026-08-04T23:20:00Z",
            }],
        }]
        selected = client.active_btc_windows(now_epoch=now_epoch)
        assert selected[0]["id"] == "live"
        assert selected[0]["_window_slug"] == current_slug
    finally:
        client.close()


def test_current_window_is_prioritized_and_closed_window_is_classified():
    now_epoch = 1_785_885_000
    current_slug = f"btc-updown-5m-{(now_epoch // 300) * 300}"
    client = PublicDataClient()
    try:
        client.get_json = lambda url, params=None: [{
            "active": True,
            "markets": [
                {
                    "id": "closed",
                    "closed": True,
                    "acceptingOrders": False,
                    "endDate": "2026-08-04T23:20:00Z",
                },
                {
                    "id": "live",
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                    "endDate": "2026-08-04T23:20:00Z",
                },
            ],
        }]
        discovery = client.discover_active_btc_windows(now_epoch=now_epoch)
        assert discovery.discovered_windows == (current_slug,)
        assert discovery.expected_closed_no_book_windows == (current_slug,)
        assert [market["id"] for market in discovery.eligible_markets] == ["live"]
        assert discovery.eligible_markets[0]["_window_slug"] == current_slug
    finally:
        client.close()


def test_clob_404_classification_depends_on_current_market_state():
    error = PublicSourceError(url="https://clob.polymarket.com/book", status_code=404, error_class="HTTPStatusError")
    now_epoch = 1_785_885_000
    ended = {"closed": False, "acceptingOrders": False, "_window_end_epoch": now_epoch - 1}
    active = {"closed": False, "acceptingOrders": True, "_window_end_epoch": now_epoch + 299}
    assert classify_book_failure(exc=error, market=ended, now_epoch=now_epoch) == "NO_ORDERBOOK_OR_CLOSED"
    assert classify_book_failure(exc=error, market=active, now_epoch=now_epoch) == "SOURCE_FAILURE"


def test_gamma_only_discovery_cannot_end_trial_early(tmp_path: Path):
    fake_clock = FakeClock(1_785_885_000)
    result = run_trial(
        output_dir=tmp_path,
        config=TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=15),
        fixture=False,
        clock=fake_clock.as_trial_clock(),
        client_factory=GammaOnlyClient,
    )
    summary = result["summary"]
    assert summary["duration_seconds"] >= 60
    assert summary["gamma_discovered_windows"] >= 1
    assert summary["valid_order_book_windows"] == 0
    assert summary["valid_order_books"] == 0
    assert summary["windows_observed"] == 0


def test_valid_books_are_counted_but_real_duration_still_controls_exit(tmp_path: Path):
    fake_clock = FakeClock(1_785_885_000)
    result = run_trial(
        output_dir=tmp_path,
        config=TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=15),
        fixture=False,
        clock=fake_clock.as_trial_clock(),
        client_factory=ValidBookClient,
    )
    summary = result["summary"]
    assert summary["duration_seconds"] >= 60
    assert summary["valid_order_book_windows"] == 1
    assert summary["valid_order_books"] == 2
    assert summary["windows_observed"] == 1
    assert summary["source_failures"] == 0
    assert summary["sequential_executions"] == 1
    assert summary["fills"] == 2
    integration = json.loads((tmp_path / "observed_runner_integration.json").read_text())
    assert integration["result"] == "PASS"
    record = integration["records"][0]
    assert record["first_and_second_snapshot_hashes_distinct"] is True
    assert record["second_source_or_receive_time_later"] is True
    assert record["delay_elapsed_ms"] >= 500
    assert record["first_leg_applied_count"] == 1
    assert record["second_leg_applied_count"] == 1
    assert record["second_leg_snapshot_required_terminal"] is False


def test_zero_mutation_of_authoritative_raw_evidence(tmp_path: Path):
    raw = tmp_path / "raw_chain_v1"
    raw.mkdir()
    evidence = raw / "manifest_000000.json"
    evidence.write_text('{"immutable":true}\n')
    before = hashlib.sha256(evidence.read_bytes()).hexdigest()
    run_trial(
        output_dir=tmp_path / "paper",
        config=TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=1),
        fixture=True,
    )
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == before


def test_paper_package_has_no_wallet_private_key_or_authenticated_client_imports():
    forbidden_import_parts = {"web3", "eth_account", "py_clob_client", "wallet", "private_key"}
    for path in sorted((ROOT / "polymarket" / "paper").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not any(part in name.lower() for name in imports for part in forbidden_import_parts), (path, imports)


def test_paper_runtime_has_no_real_order_calls_reachable():
    forbidden_calls = {"create_order", "place_order", "submit_order", "send_order", "cancel_order", "sign_order", "sign_transaction"}
    for path in sorted((ROOT / "polymarket" / "paper").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        observed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    observed.add(func.id)
                elif isinstance(func, ast.Attribute):
                    observed.add(func.attr)
        assert observed.isdisjoint(forbidden_calls), (path, observed & forbidden_calls)


def test_artifact_tampering_is_detected(tmp_path: Path):
    run_trial(output_dir=tmp_path, config=TrialConfig(duration_minutes=1, minimum_windows=1), fixture=True)
    target = tmp_path / "trial_summary.json"
    target.write_text(target.read_text() + " ")
    try:
        verify_artifact_bundle(tmp_path)
    except ValueError as exc:
        assert "digest mismatch" in str(exc)
    else:
        raise AssertionError("tampering was not detected")


class EmptySecondBookClient(ValidBookClient):
    def book(self, token_id: str):
        payload, digest = super().book(token_id)
        if token_id == "no" and self.book_calls[token_id] > 1:
            payload = {**payload, "bids": [], "asks": []}
            digest = hashlib.sha256(b"empty-second").hexdigest()
        return payload, digest


class StaleSecondBookClient(ValidBookClient):
    def book(self, token_id: str):
        payload, digest = super().book(token_id)
        if token_id == "no" and self.book_calls[token_id] > 1:
            payload = {**payload, "timestamp": str(int((self._now_epoch - 60) * 1000))}
            digest = hashlib.sha256(b"stale-second").hexdigest()
        return payload, digest


class ClosingDuringDelayClient(ValidBookClient):
    def discover_active_btc_windows(self, *, now_epoch: int) -> BtcWindowDiscovery:
        discovery = super().discover_active_btc_windows(now_epoch=now_epoch)
        market = dict(discovery.eligible_markets[0])
        market["_window_end_epoch"] = now_epoch + 0.25
        return BtcWindowDiscovery(discovery.discovered_windows, (market,), (), ())


def _run_observed_client(tmp_path: Path, client):
    fake_clock = FakeClock(1_785_885_000)
    result = run_trial(
        output_dir=tmp_path,
        config=TrialConfig(
            duration_minutes=1,
            minimum_windows=1,
            poll_seconds=15,
            slippage_bps_floor=0,
            sequential_transport_delay_ms=500,
        ),
        fixture=False,
        clock=fake_clock.as_trial_clock(),
        client_factory=lambda: client,
    )
    report = json.loads((tmp_path / "observed_runner_integration.json").read_text())
    return result, report


def test_observed_runner_empty_second_book_keeps_first_fill_and_replays(tmp_path: Path):
    client = EmptySecondBookClient()
    result, report = _run_observed_client(tmp_path, client)
    record = report["records"][0]
    assert result["summary"]["sequential_executions"] == 1
    assert result["summary"]["fills"] == 1
    assert record["completion_status"] == "SECOND_LEG_FAILED_AFTER_FIRST_FILL"
    assert record["second_leg_reason"] == "EMPTY_LIQUIDITY"
    assert record["first_leg_applied_count"] == 1
    assert record["second_leg_applied_count"] == 0
    assert record["leg_imbalance_shares"] > 0
    assert record["replay_result"] == "PASS"


def test_observed_runner_stale_second_book_keeps_first_fill(tmp_path: Path):
    client = StaleSecondBookClient()
    result, report = _run_observed_client(tmp_path, client)
    record = report["records"][0]
    assert result["summary"]["fills"] == 1
    assert record["second_leg_reason"] == "STALE_DATA"
    assert record["first_leg_applied_count"] == 1
    assert record["replay_result"] == "PASS"


def test_observed_runner_window_close_during_delay_is_terminal_without_duplicate(tmp_path: Path):
    client = ClosingDuringDelayClient()
    result, report = _run_observed_client(tmp_path, client)
    record = report["records"][0]
    assert result["summary"]["fills"] == 1
    assert record["second_leg_reason"] == "WINDOW_CLOSES_BETWEEN_LEGS"
    assert client.book_calls == {"yes": 1, "no": 1}
    assert record["first_leg_applied_count"] == 1
    assert record["replay_result"] == "PASS"


def test_repeated_poll_does_not_duplicate_first_or_second_leg(tmp_path: Path):
    client = ValidBookClient()
    result, report = _run_observed_client(tmp_path, client)
    record = report["records"][0]
    assert result["summary"]["sequential_executions"] == 1
    assert client.book_calls == {"yes": 1, "no": 2}
    assert record["first_leg_applied_count"] == 1
    assert record["second_leg_applied_count"] == 1
    assert record["no_duplicate_first_fill"] is True
    assert record["replay_result"] == "PASS"
