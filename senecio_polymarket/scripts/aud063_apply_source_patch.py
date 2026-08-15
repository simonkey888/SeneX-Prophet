#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(pattern, replacement.rstrip() + "\n\n", text, count=1, flags=re.M | re.S)
    if count != 1:
        raise SystemExit(f"expected one replacement in {path}: got {count}")
    path.write_text(new, encoding="utf-8")


SUPABASE_FETCH = r'''_pending_scan_cursor: tuple[str, str] | None = None
_pending_scan_diagnostics: dict[str, Any] = {}


def reset_pending_scan_cursor() -> None:
    """Reset the bounded keyset scan. Intended for restart semantics/tests."""
    global _pending_scan_cursor, _pending_scan_diagnostics
    _pending_scan_cursor = None
    _pending_scan_diagnostics = {}


def get_pending_scan_diagnostics() -> dict[str, Any]:
    return dict(_pending_scan_diagnostics)


async def fetch_pending_outcomes(older_than_seconds: int = 900, limit: int = 100) -> list[dict]:
    """Fetch one bounded keyset page of eligible directional NULL outcomes.

    FLAT/non-directional rows are excluded server-side. A stable (ts,id) cursor
    advances even when a row later fails historical-price/proof validation, so
    poison rows cannot permanently block later eligible rows. At end-of-pass the
    cursor resets for a later retry pass; failed rows therefore remain retryable.
    """
    global _pending_scan_cursor, _pending_scan_diagnostics
    try:
        from datetime import timedelta

        bounded_limit = max(1, min(int(limit), 500))
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        c = _get_client()
        base_params = {
            "select": "id,ts,symbol,prediction,confidence,price_now,exchange_used,audit",
            "outcome": "is.null",
            "prediction": "in.(LONG,SHORT)",
            "ts": f"lte.{cutoff}",
            "order": "ts.asc,id.asc",
        }

        # Backlog visibility is independent of the current page/cursor.
        metric_params = {
            "select": "id,ts,symbol,prediction",
            "outcome": "is.null",
            "prediction": "in.(LONG,SHORT)",
            "ts": f"lte.{cutoff}",
            "order": "ts.asc,id.asc",
            "limit": "1",
        }
        metric = await c.get(
            f"/{SUPABASE_TABLE}", params=metric_params, headers={"Prefer": "count=exact"}
        )
        eligible_count = None
        oldest = None
        if metric.status_code == 200:
            metric_rows = metric.json() or []
            if isinstance(metric_rows, list) and metric_rows:
                oldest = metric_rows[0]
            content_range = str(getattr(metric, "headers", {}).get("content-range", ""))
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[-1]
                if total.isdigit():
                    eligible_count = int(total)

        cursor_before = _pending_scan_cursor
        params = dict(base_params)
        params["limit"] = str(bounded_limit)
        if cursor_before is not None:
            cursor_ts, cursor_id = cursor_before
            params["or"] = f"(ts.gt.{cursor_ts},and(ts.eq.{cursor_ts},id.gt.{cursor_id}))"

        r = await c.get(f"/{SUPABASE_TABLE}", params=params)
        if r.status_code != 200:
            log.error("supabase fetch_pending_outcomes failed: %s %s", r.status_code, r.text[:200])
            _pending_scan_diagnostics = {
                "eligible_directional_pending_count": eligible_count,
                "oldest_eligible_directional_pending_id": (oldest or {}).get("id"),
                "oldest_eligible_directional_pending_ts": (oldest or {}).get("ts"),
                "rows_scanned_last_pass": 0,
                "scan_cap_hit": False,
                "cursor_before": cursor_before,
                "cursor_after": cursor_before,
                "pass_complete": False,
                "error": f"HTTP_{r.status_code}",
            }
            return []
        rows = r.json() or []
        rows = rows if isinstance(rows, list) else []

        pass_complete = len(rows) < bounded_limit
        cursor_after = cursor_before
        if rows:
            last = rows[-1]
            cursor_after = (str(last.get("ts") or ""), str(last.get("id") or ""))
        if pass_complete:
            _pending_scan_cursor = None
        else:
            _pending_scan_cursor = cursor_after

        _pending_scan_diagnostics = {
            "eligible_directional_pending_count": eligible_count,
            "oldest_eligible_directional_pending_id": (oldest or {}).get("id"),
            "oldest_eligible_directional_pending_ts": (oldest or {}).get("ts"),
            "rows_scanned_last_pass": len(rows),
            "scan_cap_hit": len(rows) >= bounded_limit,
            "cursor_before": cursor_before,
            "cursor_after": cursor_after,
            "pass_complete": pass_complete,
            "error": None,
        }
        return rows
    except Exception as e:
        log.error("supabase fetch_pending_outcomes error: %s", e)
        _pending_scan_diagnostics = {"error": type(e).__name__, "rows_scanned_last_pass": 0}
        return []
'''

SUPABASE_UPDATE = r'''async def update_outcome_dual(
    prediction_id: int,
    outcome_15m: str,
    outcome_1h: str,
    price_15m_later: float,
    price_1h_later: float,
    primary_window: str = "1h",
    *,
    price_evidence_15m: dict[str, Any] | None = None,
    price_evidence_1h: dict[str, Any] | None = None,
) -> bool:
    """CAS-settle one directional row only with complete causal evidence."""
    try:
        import math
        from datetime import timedelta
        from .settlement_contract import (
            WINDOW_15M_S,
            WINDOW_1H_S,
            normalize_exchange,
            normalize_symbol,
            parse_utc,
            validate_price_evidence,
        )

        c = _get_client()
        r_get = await c.get(
            f"/{SUPABASE_TABLE}",
            params={
                "select": "id,ts,symbol,prediction,price_now,exchange_used,audit,outcome",
                "id": f"eq.{prediction_id}",
                "limit": "1",
            },
        )
        if r_get.status_code != 200:
            return False
        existing_rows = r_get.json() or []
        if not isinstance(existing_rows, list) or not existing_rows:
            return False
        existing = existing_rows[0]
        if existing.get("outcome") is not None:
            return False
        direction = str(existing.get("prediction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            return False

        existing_audit = existing.get("audit") or {}
        if not isinstance(existing_audit, dict):
            try:
                existing_audit = json.loads(existing_audit) if isinstance(existing_audit, str) else {}
            except Exception:
                return False
        origin = existing_audit.get("origin_price_v1")
        if not isinstance(origin, dict) or origin.get("version") != "origin-price-v1":
            return False
        expected_source = normalize_exchange(existing.get("exchange_used"))
        if expected_source is None or normalize_exchange(origin.get("source")) != expected_source:
            return False
        row_ts = existing.get("ts")
        row_dt = parse_utc(row_ts)
        if row_dt is None or parse_utc(origin.get("timestamp")) != row_dt:
            return False
        try:
            if not math.isclose(float(origin.get("price")), float(existing.get("price_now")), rel_tol=1e-9, abs_tol=1e-9):
                return False
        except (TypeError, ValueError):
            return False

        if not validate_price_evidence(
            price_evidence_15m,
            expected_exchange=expected_source,
            expected_symbol=normalize_symbol(existing.get("symbol")),
            expected_ts=row_ts,
            expected_window_seconds=WINDOW_15M_S,
        ):
            return False
        if not validate_price_evidence(
            price_evidence_1h,
            expected_exchange=expected_source,
            expected_symbol=normalize_symbol(existing.get("symbol")),
            expected_ts=row_ts,
            expected_window_seconds=WINDOW_1H_S,
        ):
            return False
        try:
            if not math.isclose(float(price_15m_later), float(price_evidence_15m["price"]), rel_tol=1e-9, abs_tol=1e-9):
                return False
            if not math.isclose(float(price_1h_later), float(price_evidence_1h["price"]), rel_tol=1e-9, abs_tol=1e-9):
                return False
        except (TypeError, ValueError, KeyError):
            return False
        observed_at = datetime.now(timezone.utc)
        if observed_at < row_dt + timedelta(seconds=WINDOW_1H_S):
            return False

        observed_iso = observed_at.isoformat()
        outcomes_dual = {
            "outcome_15m": outcome_15m,
            "outcome_1h": outcome_1h,
            "price_15m_later": float(price_15m_later),
            "price_1h_later": float(price_1h_later),
            "primary_window": primary_window,
            "settled_at": observed_iso,
            "settlement_contract_version": "aud063-v1",
            "price_evidence_v1": {
                "15m": dict(price_evidence_15m),
                "1h": dict(price_evidence_1h),
            },
            "settlement_observation_v1": {
                "version": "settlement-observation-v1",
                "observed_at": observed_iso,
                "writer": "SENEX_PRIMARY_DUAL_WINDOW_VERIFIER_V2",
                "availability_semantics": "PERSISTED_BY_COMPARE_AND_SET_AT_OR_AFTER_THIS_TIME",
            },
        }
        merged_audit = dict(existing_audit)
        merged_audit["outcomes_dual"] = outcomes_dual
        patch_body = {
            "outcome": outcome_1h,
            "price_15m_later": float(price_15m_later),
            "audit": merged_audit,
        }
        r = await c.patch(
            f"/{SUPABASE_TABLE}",
            params={
                "id": f"eq.{prediction_id}",
                "outcome": "is.null",
                "audit->outcomes_dual": "is.null",
            },
            json=patch_body,
        )
        if r.status_code not in (200, 204):
            return False
        try:
            body = r.json() if getattr(r, "content", b"") else []
        except Exception:
            body = []
        # HTTP success with no returned changed row is a CAS no-op, not success.
        return isinstance(body, list) and len(body) > 0
    except Exception as e:
        log.error("supabase update_outcome_dual error: %s", e)
        return False
'''

ORACLE_PRICE = r'''async def _fetch_price_evidence_at_time(
    symbol: str,
    ts_iso: str,
    window_seconds: int,
    exchange_name: str,
) -> Optional[dict[str, Any]]:
    """Fetch bounded same-source historical evidence for an exact target."""
    from .settlement_contract import fetch_historical_price_evidence
    return await asyncio.to_thread(
        fetch_historical_price_evidence,
        exchange_name,
        symbol,
        ts_iso,
        window_seconds,
    )


async def _fetch_price_at_time(symbol: str, ts_iso: str, window_seconds: int = WINDOW_15M_S) -> Optional[float]:
    """Backward-compatible OKX helper; authoritative verifier uses evidence API."""
    evidence = await _fetch_price_evidence_at_time(symbol, ts_iso, window_seconds, "okx")
    return float(evidence["price"]) if evidence else None
'''

ORACLE_OUTCOME = r'''def _outcome_for_direction(direction: str, price_now: float, price_later: float) -> Optional[str]:
    from .settlement_contract import directional_outcome
    return directional_outcome(direction, price_now, price_later)
'''

ORACLE_VERIFY = r'''async def _verify_pending_outcomes() -> int:
    """Settle one bounded, starvation-safe page of directional predictions."""
    try:
        from . import supabase_client
        from .settlement_contract import normalize_exchange, normalize_symbol, target_epoch_ms
    except Exception as e:
        log.warning("settlement dependencies unavailable, skipping verifier: %s", e)
        return 0

    try:
        pending = await supabase_client.fetch_pending_outcomes(
            older_than_seconds=WINDOW_1H_S, limit=100
        )
        scan = supabase_client.get_pending_scan_diagnostics()
    except Exception as e:
        log.exception("fetch_pending_outcomes failed: %s", e)
        return 0

    _state["eligible_directional_pending_count"] = scan.get("eligible_directional_pending_count")
    _state["oldest_eligible_directional_pending_id"] = scan.get("oldest_eligible_directional_pending_id")
    _state["oldest_eligible_directional_pending_ts"] = scan.get("oldest_eligible_directional_pending_ts")
    _state["last_verify_rows_scanned"] = scan.get("rows_scanned_last_pass", len(pending))
    _state["last_verify_scan_cap_hit"] = bool(scan.get("scan_cap_hit"))
    _state["last_verify_cursor"] = scan.get("cursor_after")
    _state["last_verify_scan_pass_complete"] = bool(scan.get("pass_complete"))
    oldest_ts = scan.get("oldest_eligible_directional_pending_ts")
    try:
        oldest_dt = datetime.fromisoformat(str(oldest_ts).replace("Z", "+00:00"))
        _state["oldest_eligible_directional_pending_age_seconds"] = max(
            0.0, (datetime.now(timezone.utc) - oldest_dt).total_seconds()
        )
    except Exception:
        _state["oldest_eligible_directional_pending_age_seconds"] = None

    settled = 0
    settled_ids: list[int] = []
    unresolved_proof = 0
    unresolved_price = 0
    cas_conflicts = 0
    cache: dict[tuple[str, str, int, int], Optional[dict[str, Any]]] = {}

    for row in pending:
        pred_id = row.get("id")
        direction = str(row.get("prediction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            unresolved_proof += 1
            continue
        ts_iso = row.get("ts")
        try:
            price_now = float(row.get("price_now") or 0)
        except (TypeError, ValueError):
            price_now = 0.0
        if not ts_iso or price_now <= 0:
            unresolved_proof += 1
            continue

        audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        origin = audit.get("origin_price_v1") if isinstance(audit, dict) else None
        row_source = normalize_exchange(row.get("exchange_used"))
        if (
            not isinstance(origin, dict)
            or origin.get("version") != "origin-price-v1"
            or row_source is None
            or normalize_exchange(origin.get("source")) != row_source
        ):
            unresolved_proof += 1
            continue

        symbol = normalize_symbol(row.get("symbol"))
        evidence: dict[str, Optional[dict[str, Any]]] = {}
        for name, window_s in (("15m", WINDOW_15M_S), ("1h", WINDOW_1H_S)):
            target = target_epoch_ms(ts_iso, window_s)
            if target is None:
                evidence[name] = None
                continue
            key = (row_source, symbol, target, window_s)
            if key not in cache:
                cache[key] = await _fetch_price_evidence_at_time(
                    symbol, str(ts_iso), window_s, row_source
                )
                await asyncio.sleep(0.05)
            evidence[name] = cache[key]

        ev15 = evidence.get("15m")
        ev1h = evidence.get("1h")
        if not ev15 or not ev1h:
            unresolved_price += 1
            continue
        price_15m = float(ev15["price"])
        price_1h = float(ev1h["price"])
        outcome_15m = _outcome_for_direction(direction, price_now, price_15m)
        outcome_1h = _outcome_for_direction(direction, price_now, price_1h)
        if outcome_15m is None or outcome_1h is None:
            unresolved_proof += 1
            continue

        ok = await supabase_client.update_outcome_dual(
            prediction_id=pred_id,
            outcome_15m=outcome_15m,
            outcome_1h=outcome_1h,
            price_15m_later=price_15m,
            price_1h_later=price_1h,
            primary_window=PRIMARY_WINDOW,
            price_evidence_15m=ev15,
            price_evidence_1h=ev1h,
        )
        if ok:
            settled += 1
            if len(settled_ids) < 10:
                settled_ids.append(pred_id)
        else:
            cas_conflicts += 1
        await asyncio.sleep(0.02)

    _state["last_verify_at"] = datetime.now(timezone.utc).isoformat()
    _state["last_verify_count"] = settled
    _state["last_verify_ids"] = settled_ids
    _state["verified_total"] = (_state.get("verified_total") or 0) + settled
    _state["last_verify_unresolved_proof"] = unresolved_proof
    _state["last_verify_unresolved_price"] = unresolved_price
    _state["last_verify_cas_conflicts"] = cas_conflicts
    _state["last_verify_unresolved_due_price_or_proof"] = unresolved_proof + unresolved_price
    if pending and settled == 0:
        if unresolved_proof:
            reason = "ELIGIBLE_PENDING_MISSING_CAUSAL_PROOF"
        elif unresolved_price:
            reason = "ELIGIBLE_PENDING_HISTORICAL_PRICE_UNAVAILABLE"
        elif cas_conflicts:
            reason = "ELIGIBLE_PENDING_CAS_CONFLICT_OR_ALREADY_SETTLED"
        else:
            reason = "ELIGIBLE_PENDING_NO_PROGRESS"
    elif not pending and scan.get("eligible_directional_pending_count"):
        reason = "SCAN_PASS_BOUNDARY_CURSOR_RESET"
    else:
        reason = None
    _state["last_verify_no_progress_reason"] = reason

    log.info(
        "verifier aud063: scanned=%d settled=%d proof_unresolved=%d price_unresolved=%d cas=%d cap=%s",
        len(pending), settled, unresolved_proof, unresolved_price, cas_conflicts,
        bool(scan.get("scan_cap_hit")),
    )
    await _refresh_directional_stats()
    if settled > 0:
        try:
            from .forensics import pipeline as forensics_pipeline
            asyncio.create_task(forensics_pipeline.run_pipeline_async())
        except Exception as f_err:
            log.warning("forensics pipeline scheduling failed (continuing): %s", f_err)
    return settled
'''


def main() -> None:
    supabase = ROOT / "backend" / "supabase_client.py"
    replace(
        supabase,
        r'^async def fetch_pending_outcomes\(.*?(?=^async def update_outcome_dual\()',
        SUPABASE_FETCH,
    )
    replace(
        supabase,
        r'^async def update_outcome_dual\(.*?(?=^async def close\()',
        SUPABASE_UPDATE,
    )

    runner = ROOT / "backend" / "oracle_runner.py"
    replace(
        runner,
        r'^async def _fetch_price_at_time\(.*?(?=^def _outcome_for_direction\()',
        ORACLE_PRICE,
    )
    replace(
        runner,
        r'^def _outcome_for_direction\(.*?(?=^async def _verify_pending_outcomes\()',
        ORACLE_OUTCOME,
    )
    replace(
        runner,
        r'^async def _verify_pending_outcomes\(.*?(?=^async def _backfill_bogus_outcomes\()',
        ORACLE_VERIFY,
    )

    print("AUD063_SOURCE_PATCH_APPLIED")


if __name__ == "__main__":
    main()
