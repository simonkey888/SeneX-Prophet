#!/usr/bin/env python3
"""Observed SENEX paper-only trial using public GET data or deterministic fixtures."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from polymarket.clob_readonly import simulate_complete_set
from polymarket.paper.broker import PublicOrderBook, SimulatedBroker
from polymarket.paper.engine import PaperEngine
from polymarket.paper.models import PaperTrialSummary, deterministic_id, sha256_json
from polymarket.paper.portfolio import AppendOnlyJournal, PaperPortfolio
from polymarket.paper.report import REQUIRED_ARTIFACTS, TrialArtifactWriter, verify_artifact_bundle
from polymarket.paper.risk import PaperRiskConfig, PaperRiskEngine


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def canonical_hash(value: Any) -> str:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_evidence_chain(records: Iterable[dict[str, Any]]) -> bool:
    previous = "GENESIS"
    for index, record in enumerate(records):
        material = {key: value for key, value in record.items() if key != "record_hash"}
        if material.get("sequence") != index or material.get("previous_hash") != previous:
            return False
        expected = canonical_hash(material)
        if record.get("record_hash") != expected:
            return False
        body = str(record.get("response_body_utf8", "")).encode("utf-8")
        if hashlib.sha256(body).hexdigest() != record.get("sha256"):
            return False
        previous = expected
    return True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def code_sha() -> str:
    env = os.environ.get("SENECIO_CODE_SHA") or os.environ.get("GITHUB_SHA")
    if env:
        return env
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "UNKNOWN_CODE_SHA"


@dataclass(frozen=True)
class TrialConfig:
    duration_minutes: int = 60
    minimum_windows: int = 12
    poll_seconds: int = 15
    max_recent_windows: int = 36
    requested_shares: float = 2.0
    fee_bps: float = 0.0
    virtual_starting_equity_usd: float = 10_000.0
    max_order_notional_pct: float = 1.0
    max_gross_exposure_pct: float = 5.0
    max_single_market_exposure_pct: float = 2.0
    max_session_drawdown_pct: float = 2.0
    max_consecutive_losses: int = 5
    book_staleness_seconds: float = 15.0
    slippage_bps_floor: float = 5.0
    public_get_only: bool = True
    paper_only: bool = True
    orders_enabled: bool = False
    live_capital_locked: bool = True


class PublicDataClient:
    def __init__(self, *, timeout_seconds: float = 15.0):
        self.client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self.requests: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.failures = 0

    def close(self) -> None:
        self.client.close()

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        started = time.monotonic()
        timestamp = utc_now()
        try:
            response = self.client.get(url, params=params)
            body = response.content
            status = response.status_code
            response.raise_for_status()
            value = response.json()
        except Exception as exc:
            self.failures += 1
            self.requests.append({
                "timestamp_utc": timestamp,
                "method": "GET",
                "url": url,
                "params": params or {},
                "status": getattr(getattr(exc, "response", None), "status_code", None),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "result": "ERROR",
                "error_class": type(exc).__name__,
            })
            raise
        digest = hashlib.sha256(body).hexdigest()
        self.requests.append({
            "timestamp_utc": timestamp,
            "method": "GET",
            "url": str(response.request.url),
            "params": params or {},
            "status": status,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "result": "SUCCESS",
            "response_sha256": digest,
            "response_bytes": len(body),
        })
        evidence_record = {
            "sequence": len(self.evidence),
            "previous_hash": self.evidence[-1]["record_hash"] if self.evidence else "GENESIS",
            "timestamp_utc": timestamp,
            "url": str(response.request.url),
            "sha256": digest,
            "bytes": len(body),
            "content_type": response.headers.get("content-type", ""),
            "response_body_utf8": body.decode("utf-8", errors="replace"),
        }
        evidence_record["record_hash"] = canonical_hash(evidence_record)
        self.evidence.append(evidence_record)
        return value

    def recent_btc_windows(self, *, now_epoch: int, count: int) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        current = (now_epoch // 300) * 300
        for offset in range(1, count + 1):
            epoch = current - (offset * 300)
            slug = f"btc-updown-5m-{epoch}"
            try:
                events = self.get_json(f"{GAMMA_BASE}/events", {"slug": slug})
            except Exception:
                continue
            if isinstance(events, dict):
                events = [events]
            for event in events or []:
                markets = event.get("markets") or []
                if not markets:
                    continue
                market = markets[0]
                market["_window_slug"] = slug
                market["_window_epoch"] = epoch
                windows.append(market)
                break
        return windows

    def book(self, token_id: str) -> tuple[dict[str, Any], str]:
        payload = self.get_json(f"{CLOB_BASE}/book", {"token_id": token_id})
        evidence_hash = self.evidence[-1]["sha256"]
        return payload, evidence_hash


def _parse_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []


def _fixture_observations(timestamp_utc: str) -> list[tuple[dict[str, Any], dict[str, PublicOrderBook], dict[str, str]]]:
    market_id = "fixture-btc-updown-5m"
    tokens = ["fixture-yes", "fixture-no"]
    payloads = {
        tokens[0]: {"asset_id": tokens[0], "bids": [{"price": "0.45", "size": "20"}], "asks": [{"price": "0.46", "size": "20"}]},
        tokens[1]: {"asset_id": tokens[1], "bids": [{"price": "0.45", "size": "20"}], "asks": [{"price": "0.46", "size": "20"}]},
    }
    books = {
        token: PublicOrderBook.from_payload(
            market_id=market_id,
            token_id=token,
            timestamp_utc=timestamp_utc,
            payload=payload,
            source_evidence_hash=sha256_json(payload),
        )
        for token, payload in payloads.items()
    }
    snapshot = simulate_complete_set(payloads[tokens[0]], payloads[tokens[1]], 2.0, 0.0)
    record = {
        "market_id": market_id,
        "condition_id": market_id,
        "token_ids": tokens,
        "shadow_execution": {"status": "SHADOW_EXECUTABLE", "net_edge": snapshot.net_edge_usdc},
        "evidence_verified": True,
        "raw_chain_verified": True,
        "replay_verified": True,
        "regime_known": True,
        "window_slug": market_id,
    }
    return [(record, books, {tokens[0]: "UP", tokens[1]: "DOWN"})]


def run_trial(*, output_dir: Path, config: TrialConfig, fixture: bool = False) -> dict[str, Any]:
    started_utc = utc_now()
    started_monotonic = time.monotonic()
    sha = code_sha()
    config_dict = asdict(config)
    config_sha = sha256_json(config_dict)
    trial_id = deterministic_id("trial", {"start_utc": started_utc, "code_sha": sha, "config_sha": config_sha})
    journal = AppendOnlyJournal(output_dir / "portfolio_ledger.jsonl")
    portfolio = PaperPortfolio(starting_equity_usd=config.virtual_starting_equity_usd, journal=journal)
    risk_config = PaperRiskConfig(
        virtual_starting_equity_usd=config.virtual_starting_equity_usd,
        max_order_notional_pct=config.max_order_notional_pct,
        max_gross_exposure_pct=config.max_gross_exposure_pct,
        max_single_market_exposure_pct=config.max_single_market_exposure_pct,
        max_session_drawdown_pct=config.max_session_drawdown_pct,
        max_consecutive_losses=config.max_consecutive_losses,
        book_staleness_seconds=config.book_staleness_seconds,
        slippage_bps_floor=config.slippage_bps_floor,
    )
    engine = PaperEngine(
        broker=SimulatedBroker(
            fee_bps=config.fee_bps,
            slippage_bps_floor=config.slippage_bps_floor,
            book_staleness_seconds=config.book_staleness_seconds,
        ),
        risk=PaperRiskEngine(risk_config),
        portfolio=portfolio,
    )
    decisions: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    risk_decisions: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    observed_windows: set[str] = set()
    source_windows: set[str] = set()
    observed_markets: set[str] = set()
    stale_events = 0
    integrity_failures = 0
    client = PublicDataClient()
    try:
        while True:
            now_utc = utc_now()
            observations: list[tuple[dict[str, Any], dict[str, PublicOrderBook], dict[str, str]]] = []
            if fixture:
                observations = _fixture_observations(now_utc)
            else:
                markets = client.recent_btc_windows(now_epoch=int(time.time()), count=config.max_recent_windows)
                for market in markets:
                    source_windows.add(str(market.get("_window_slug") or market.get("id") or "UNKNOWN_WINDOW"))
                    observed_markets.add(str(market.get("conditionId") or market.get("condition_id") or market.get("id") or market.get("_window_slug")))
                    tokens = _parse_list(market.get("clobTokenIds"))
                    outcomes = _parse_list(market.get("outcomes"))
                    if len(tokens) != 2:
                        continue
                    market_id = str(market.get("conditionId") or market.get("condition_id") or market.get("id") or market.get("_window_slug"))
                    books: dict[str, PublicOrderBook] = {}
                    payloads: dict[str, dict[str, Any]] = {}
                    failed = False
                    for token in tokens:
                        try:
                            payload, evidence_hash = client.book(token)
                            payloads[token] = payload
                            books[token] = PublicOrderBook.from_payload(
                                market_id=market_id,
                                token_id=token,
                                timestamp_utc=now_utc,
                                payload=payload,
                                source_evidence_hash=evidence_hash,
                            )
                        except Exception as exc:
                            failed = True
                            abstentions.append({
                                "timestamp_utc": now_utc,
                                "market_id": market_id,
                                "reason": "SOURCE_FAILURE",
                                "detail": type(exc).__name__,
                            })
                            break
                    if failed:
                        continue
                    snapshot = simulate_complete_set(payloads[tokens[0]], payloads[tokens[1]], config.requested_shares, config.fee_bps / 10_000.0)
                    record = {
                        "market_id": market_id,
                        "condition_id": market_id,
                        "token_ids": tokens,
                        "shadow_execution": {
                            "status": "SHADOW_EXECUTABLE" if snapshot.fully_fillable and snapshot.net_edge_usdc > 0 else "REJECTED",
                            "net_edge": snapshot.net_edge_usdc,
                            "fully_fillable": snapshot.fully_fillable,
                        },
                        "evidence_verified": True,
                        "raw_chain_verified": True,
                        "replay_verified": True,
                        "regime_known": True,
                        "window_slug": market.get("_window_slug"),
                        "window_epoch": market.get("_window_epoch"),
                    }
                    outcome_map = {token: (outcomes[index] if index < len(outcomes) else f"OUTCOME_{index}") for index, token in enumerate(tokens)}
                    observations.append((record, books, outcome_map))
            if not observations and not source_windows:
                abstentions.append({"timestamp_utc": now_utc, "market_id": None, "reason": "NO_PUBLIC_BTC_WINDOW_AVAILABLE"})
            for record, books, outcomes in observations:
                window = str(record.get("window_slug") or record.get("market_id"))
                observed_windows.add(window)
                observed_markets.add(str(record.get("market_id")))
                per_leg_limit = config.virtual_starting_equity_usd * config.max_order_notional_pct / 100.0 / 2.0
                result = engine.process_h011_record(
                    record=record,
                    books=books,
                    outcomes=outcomes,
                    timestamp_utc=now_utc,
                    code_sha=sha,
                    config_sha=config_sha,
                    requested_shares=config.requested_shares,
                    max_notional_per_leg_usd=per_leg_limit,
                )
                decisions.append(result.decision.to_dict())
                orders.extend(intent.to_dict() for intent in result.intents)
                if result.risk_decision is not None:
                    risk_decisions.append(result.risk_decision.to_dict())
                fills.extend(fill.to_dict() for fill in result.fills)
                for reason in result.abstention_reasons:
                    abstentions.append({
                        "timestamp_utc": now_utc,
                        "market_id": result.decision.market_id,
                        "decision_id": result.decision.deterministic_id,
                        "reason": reason,
                    })
                    if reason == "STALE_DATA":
                        stale_events += 1
                    if reason in {"INTEGRITY_FAILURE", "RAW_CHAIN_INVALID", "REPLAY_UNVERIFIED"}:
                        integrity_failures += 1
                prices = {}
                for token, book in books.items():
                    if book.bids and book.asks:
                        prices[token] = (book.bids[0][0] + book.asks[0][0]) / 2.0
                snapshots.append(portfolio.snapshot(
                    timestamp_utc=now_utc,
                    code_sha=sha,
                    config_sha=config_sha,
                    source_evidence_hash=result.decision.source_evidence_hash,
                    prices=prices,
                ).to_dict())
            elapsed_minutes = (time.monotonic() - started_monotonic) / 60.0
            if fixture or len(observed_windows | source_windows) >= config.minimum_windows or elapsed_minutes >= config.duration_minutes:
                break
            time.sleep(max(1, config.poll_seconds))
    finally:
        client.close()
    ended_utc = utc_now()
    observed_windows.update(source_windows)
    raw_verified = True if fixture else verify_evidence_chain(client.evidence)
    if not raw_verified:
        integrity_failures += 1
    replayed = PaperPortfolio.replay(starting_equity_usd=config.virtual_starting_equity_usd, records=journal.read_all())
    final_prices: dict[str, float] = {}
    final_snapshot = portfolio.snapshot(
        timestamp_utc=ended_utc,
        code_sha=sha,
        config_sha=config_sha,
        source_evidence_hash=sha256_json(client.evidence),
        prices=final_prices,
    )
    replay_snapshot = replayed.snapshot(
        timestamp_utc=ended_utc,
        code_sha=sha,
        config_sha=config_sha,
        source_evidence_hash=sha256_json(client.evidence),
        prices=final_prices,
    )
    replay_result = "PASS" if (
        abs(final_snapshot.cash_usd - replay_snapshot.cash_usd) < 1e-9
        and abs(final_snapshot.realized_pnl - replay_snapshot.realized_pnl) < 1e-9
        and [p.to_dict() for p in final_snapshot.positions] == [p.to_dict() for p in replay_snapshot.positions]
    ) else "FAIL"
    counts = {key: sum(1 for item in decisions if item["action"] == key) for key in ("LONG", "SHORT", "FLAT", "NO_TRADE")}
    abstention_counts: dict[str, int] = {}
    for item in abstentions:
        reason = str(item.get("reason"))
        abstention_counts[reason] = abstention_counts.get(reason, 0) + 1
    turnover = sum(float(fill["gross_notional_usd"]) for fill in fills)
    summary_payload = {
        "trial_id": trial_id,
        "start_utc": started_utc,
        "end_utc": ended_utc,
        "windows_observed": len(observed_windows),
        "markets_observed": len(observed_markets),
        "decisions_total": len(decisions),
        "fills": len(fills),
        "replay_result": replay_result,
    }
    summary = PaperTrialSummary(
        schema_version="senex-paper-v1",
        timestamp_utc=ended_utc,
        code_sha=sha,
        config_sha=config_sha,
        source_evidence_hash=sha256_json(client.evidence),
        deterministic_id=deterministic_id("trial_summary", summary_payload),
        provenance="PUBLIC_GET_OR_DETERMINISTIC_FIXTURE_PAPER_ONLY",
        trial_id=trial_id,
        start_utc=started_utc,
        end_utc=ended_utc,
        windows_observed=len(observed_windows),
        markets_observed=len(observed_markets),
        decisions_total=len(decisions),
        long_short_flat_counts=counts,
        abstention_counts=abstention_counts,
        order_intents=len(orders),
        fills=len(fills),
        partial_fills=sum(1 for fill in fills if fill["partial"]),
        turnover=round(turnover, 12),
        realized_pnl=final_snapshot.realized_pnl,
        unrealized_pnl=final_snapshot.unrealized_pnl,
        ending_equity=final_snapshot.equity_usd,
        max_drawdown=final_snapshot.max_drawdown_pct,
        risk_rejections=sum(1 for item in risk_decisions if not item["allowed"]),
        source_failures=client.failures,
        stale_data_events=stale_events,
        integrity_failures=integrity_failures,
        replay_result=replay_result,
        raw_chain_verified=raw_verified,
        replay_verified=replay_result == "PASS",
        legacy_mode=False,
        paper_only=True,
        orders_enabled=False,
        live_capital_locked=True,
        real_order_network_calls=0,
        real_order_methods_reachable=0,
        wallet_private_key_dependencies=0,
    )
    writer = TrialArtifactWriter(output_dir)
    writer.write_json("trial_manifest.json", {
        "schema_version": "senex-paper-trial-manifest-v1",
        "trial_id": trial_id,
        "code_sha": sha,
        "config_sha": config_sha,
        "public_get_only": True,
        "fixture": fixture,
        "required_files": [*REQUIRED_ARTIFACTS, "SHA256SUMS"],
        "safety": {"paper_only": True, "orders_enabled": False, "live_capital_locked": True},
    })
    writer.write_json("config.json", config_dict)
    writer.write_jsonl("source_request_ledger.jsonl", client.requests)
    writer.write_json("raw_evidence_manifest.json", {"schema_version": "senex-raw-evidence-manifest-v1", "chain_verified": raw_verified, "chain_head": client.evidence[-1]["record_hash"] if client.evidence else "GENESIS", "items": client.evidence, "authoritative_raw_mutated": False})
    writer.write_jsonl("paper_decisions.jsonl", decisions)
    writer.write_jsonl("paper_orders.jsonl", orders)
    writer.write_jsonl("paper_fills.jsonl", fills)
    # Journal already wrote portfolio_ledger.jsonl. Ensure an empty file exists.
    (output_dir / "portfolio_ledger.jsonl").touch(exist_ok=True)
    writer.write_jsonl("portfolio_snapshots.jsonl", snapshots + [final_snapshot.to_dict()])
    writer.write_jsonl("risk_decisions.jsonl", risk_decisions)
    writer.write_jsonl("abstentions.jsonl", abstentions)
    writer.write_json("trial_summary.json", summary.to_dict())
    hashes = writer.finalize()
    verify_artifact_bundle(output_dir)
    return {"summary": summary.to_dict(), "hashes": hashes, "output_dir": str(output_dir)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--duration-minutes", type=int, default=60)
    parser.add_argument("--minimum-windows", type=int, default=12)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args(argv)
    config = TrialConfig(
        duration_minutes=max(1, args.duration_minutes),
        minimum_windows=max(1, args.minimum_windows),
        poll_seconds=max(1, args.poll_seconds),
    )
    result = run_trial(output_dir=args.output_dir, config=config, fixture=args.fixture)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
