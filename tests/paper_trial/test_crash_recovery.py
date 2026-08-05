from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from paper.trial_runner import BtcWindowDiscovery, TrialClock, TrialConfig, run_trial


class CrashClock:
    def __init__(self, epoch: float):
        self.epoch_value = float(epoch)
        self.monotonic_value = 0.0

    def monotonic(self) -> float:
        return self.monotonic_value

    def epoch(self) -> float:
        return self.epoch_value

    def sleep(self, seconds: float) -> None:
        self.monotonic_value += float(seconds)
        self.epoch_value += float(seconds)

    def now_utc(self) -> str:
        return datetime.fromtimestamp(self.epoch_value, timezone.utc).isoformat().replace("+00:00", "Z")

    def trial_clock(self) -> TrialClock:
        return TrialClock(self.monotonic, self.epoch, self.sleep, self.now_utc)


class RestartClient:
    def __init__(self, clock: CrashClock):
        self.clock = clock
        self.requests = []
        self.evidence = []
        self.failures = 0
        self.book_calls: dict[str, int] = {}

    def close(self) -> None:
        return None

    def discover_active_btc_windows(self, *, now_epoch: int) -> BtcWindowDiscovery:
        slug = "btc-updown-5m-crash-recovery"
        market = {
            "id": slug,
            "conditionId": slug,
            "closed": False,
            "acceptingOrders": True,
            "active": True,
            "clobTokenIds": ["yes", "no"],
            "outcomes": ["UP", "DOWN"],
            "_window_slug": slug,
            "_window_epoch": now_epoch,
            "_window_end_epoch": now_epoch + 300,
        }
        return BtcWindowDiscovery((slug,), (market,), (), ())

    def market_info(self, condition_id: str):
        payload = {"condition_id": condition_id, "fd": {"r": 0, "e": 2, "to": True}, "itode": False}
        return payload, hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def book(self, token_id: str):
        count = self.book_calls.get(token_id, 0) + 1
        self.book_calls[token_id] = count
        payload = {
            "asset_id": token_id,
            "timestamp": str(int(self.clock.epoch() * 1000)),
            "bids": [{"price": "0.45", "size": "20"}],
            "asks": [{"price": "0.56" if token_id == "no" and count > 1 else "0.46", "size": "20"}],
        }
        return payload, hashlib.sha256(f"{token_id}:{count}:{payload['timestamp']}".encode()).hexdigest()


class InjectedCrash(RuntimeError):
    pass


def _run_with_crash(tmp_path: Path, fault_point: str):
    clock = CrashClock(1_785_844_800.0)
    client = RestartClient(clock)
    fired = False

    def injector(point: str) -> None:
        nonlocal fired
        if point == fault_point and not fired:
            fired = True
            raise InjectedCrash(point)

    config = TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=15, slippage_bps_floor=0.0)
    with pytest.raises(InjectedCrash):
        run_trial(
            output_dir=tmp_path,
            config=config,
            fixture=False,
            clock=clock.trial_clock(),
            client_factory=lambda: client,
            code_sha_override="a" * 40,
            fault_injector=injector,
        )
    result = run_trial(
        output_dir=tmp_path,
        config=config,
        fixture=False,
        clock=clock.trial_clock(),
        client_factory=lambda: client,
        code_sha_override="a" * 40,
    )
    report = json.loads((tmp_path / "crash_recovery_report.json").read_text())
    ledger = [json.loads(line) for line in (tmp_path / "portfolio_ledger.jsonl").read_text().splitlines() if line.strip()]
    fills = [item["fill"] for item in ledger if item.get("type") == "PAPER_FILL"]
    return result, report, client, fills


@pytest.mark.parametrize(
    "fault_point",
    [
        "AFTER_FIRST_STATE_DURABLE",
        "AFTER_FIRST_FILL_DURABLE",
        "AFTER_FIRST_COMMIT_DURABLE",
        "AFTER_SECOND_STATE_DURABLE",
        "AFTER_SECOND_FILL_DURABLE",
        "AFTER_TERMINAL_DURABLE",
    ],
)
def test_exactly_once_across_all_durable_restart_boundaries(tmp_path: Path, fault_point: str):
    result, report, client, fills = _run_with_crash(tmp_path, fault_point)
    assert result["summary"]["fills"] == 2
    assert len({fill["deterministic_id"] for fill in fills}) == 2
    assert len(fills) == 2
    assert client.book_calls == {"yes": 1, "no": 2}
    assert report["first_leg_exactly_once_across_restart"] is True
    assert report["second_leg_exactly_once_across_restart"] is True
    assert report["portfolio_replay_result"] == "PASS"
    assert report["orchestration_replay_result"] == "PASS"
    assert report["sequential_result_replay_result"] == "PASS"


def test_second_restart_after_terminal_has_no_new_books_or_fills(tmp_path: Path):
    result, report, client, fills = _run_with_crash(tmp_path, "AFTER_TERMINAL_DURABLE")
    before_calls = dict(client.book_calls)
    before_ids = [fill["deterministic_id"] for fill in fills]
    clock = CrashClock(1_785_844_900.0)
    run_trial(
        output_dir=tmp_path,
        config=TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=15, slippage_bps_floor=0.0),
        fixture=False,
        clock=clock.trial_clock(),
        client_factory=lambda: client,
        code_sha_override="a" * 40,
    )
    ledger = [json.loads(line) for line in (tmp_path / "portfolio_ledger.jsonl").read_text().splitlines() if line.strip()]
    after_ids = [item["fill"]["deterministic_id"] for item in ledger if item.get("type") == "PAPER_FILL"]
    assert client.book_calls == before_calls
    assert after_ids == before_ids
    assert report["orchestration_replay_result"] == "PASS"


def test_explicit_code_sha_overrides_ambient_merge_ref(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    clock = CrashClock(1_785_844_800.0)
    client = RestartClient(clock)
    exact = "e" * 40
    result = run_trial(
        output_dir=tmp_path,
        config=TrialConfig(duration_minutes=1, minimum_windows=1, poll_seconds=15, slippage_bps_floor=0.0),
        fixture=False,
        clock=clock.trial_clock(),
        client_factory=lambda: client,
        code_sha_override=exact,
    )
    integration = json.loads((tmp_path / "observed_runner_integration.json").read_text())
    assert result["summary"]["code_sha"] == exact
    assert integration["code_sha"] == exact
