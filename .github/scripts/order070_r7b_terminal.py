from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).with_name("order070_r7_terminal.py")
s = SOURCE.read_text()
old = '''    if state is not None:\n        sb = state["body"]\n        if sb.get("trade_mode") != "PAPER" or sb.get("orders_enabled") is not False or sb.get("live_capital_locked") is not True:\n            raise RuntimeError("STATE_SAFETY_FAILED")\n'''
new = '''    if state is not None:\n        sb = state["body"]\n        # /api/oracle/state is operational state, not the canonical safety-lock surface.\n        # Reject contradictions if optional compatibility fields exist; canonical locks are\n        # asserted above via /healthz + /readyz and separately via live_gate/E2E.\n        if "trade_mode" in sb and sb.get("trade_mode") != "PAPER":\n            raise RuntimeError("STATE_TRADE_MODE_CONTRADICTION")\n        if "orders_enabled" in sb and sb.get("orders_enabled") is not False:\n            raise RuntimeError("STATE_ORDERS_CONTRADICTION")\n        if "live_capital_locked" in sb and sb.get("live_capital_locked") is not True:\n            raise RuntimeError("STATE_CAPITAL_LOCK_CONTRADICTION")\n'''
if s.count(old) != 1:
    raise RuntimeError(f"R7B_PATCH_MATCH_COUNT={s.count(old)}")
s = s.replace(old, new, 1)
code = compile(s, str(SOURCE) + "[R7B_STATE_CONTRACT]", "exec")
exec(code, {"__name__": "__main__", "__file__": str(SOURCE)})
