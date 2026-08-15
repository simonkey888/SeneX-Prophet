#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SUPABASE_BLOCK = '''_pending_scan_cursor: tuple[str, str] | None = None
_pending_scan_diagnostics: dict[str, Any] = {}


def reset_pending_scan_cursor() -> None:
    """Reset the bounded keyset scan. Intended for restart semantics/tests."""
    global _pending_scan_cursor, _pending_scan_diagnostics
    _pending_scan_cursor = None
    _pending_scan_diagnostics = {}


def get_pending_scan_diagnostics() -> dict[str, Any]:
    return dict(_pending_scan_diagnostics)'''

ORACLE_BLOCK = '''async def _fetch_price_evidence_at_time(
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
    )'''


def collapse(path: Path, block: str) -> int:
    text = path.read_text(encoding="utf-8")
    needle = block + "\n\n\n" + block
    changes = 0
    while needle in text:
        text = text.replace(needle, block, 1)
        changes += 1
    path.write_text(text, encoding="utf-8")
    return changes


def main() -> None:
    changes = 0
    changes += collapse(ROOT / "backend" / "supabase_client.py", SUPABASE_BLOCK)
    changes += collapse(ROOT / "backend" / "oracle_runner.py", ORACLE_BLOCK)
    print(f"AUD063_DEDUP_CHANGES={changes}")


if __name__ == "__main__":
    main()
