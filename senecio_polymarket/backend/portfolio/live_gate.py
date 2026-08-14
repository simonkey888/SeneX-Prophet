"""
SENECIO ORACLE — ACT XXV: LIVE_GATE Evaluator
==============================================

Hard unlock gate that must be satisfied before the ExecutionEngine is
allowed to place real (non-paper) orders. Per ACT-XXV spec, all 6 of the
following conditions must be met simultaneously:

  1. global_win_rate >= 52%            (from /api/oracle/score)
  2. verified >= 300                   (sample-size floor)
  3. profit_factor > 1.20              (from PortfolioAnalytics)
  4. max_drawdown < 10%                (from PortfolioAnalytics)
  5. shadow_live_passed                (from ShadowLive.generate_report())
  6. execution_engine_verified         (from ExecutionEngine self-test)

Until ALL 6 pass, the system stays in PAPER mode with
live_capital_locked=True. The gate is RE-EVALUATED on every call to
evaluate() — if any condition later fails (e.g. drawdown spikes), the
gate re-locks automatically and the ExecutionEngine is forced back to
PAPER.

Usage:
    gate = LiveGate()
    # whenever you want to check:
    status = gate.evaluate(
        oracle_score=oracle_score_dict,
        analytics_report=analytics.compute(trades),
        shadow_report=shadow.generate_report(),
        exec_self_test=engine.self_test(),
    )
    if status.unlocked:
        engine.enable_live_mode(unlocked_by="LIVE_GATE")
    else:
        log.warning("LIVE_GATE locked: %s", status.failed_reasons)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("senecio.live_gate")


# Per ACT-XXV spec
UNLOCK_CONDITIONS = {
    "global_win_rate_pct": {"op": ">=", "value": 52.0},
    "verified":            {"op": ">=", "value": 300},
    "profit_factor":       {"op": ">",  "value": 1.20},
    "max_drawdown_pct":    {"op": "<",  "value": 10.0},
    "shadow_live_passed":  {"op": "==", "value": True},
    "execution_engine_verified": {"op": "==", "value": True},
}


@dataclass
class GateStatus:
    """Result of LiveGate.evaluate()."""
    unlocked: bool                          # True only if ALL 6 conditions pass
    trade_mode: str = "PAPER"               # "PAPER" unless unlocked
    live_capital_locked: bool = True        # True unless unlocked
    conditions: dict = field(default_factory=dict)  # name → {value, threshold, op, pass}
    failed_reasons: list[str] = field(default_factory=list)
    evaluated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LiveGate:
    """Evaluates the 6 ACT-XXV LIVE_GATE unlock conditions.

    The gate is stateless between calls — every evaluate() reads the
    current state of all upstream systems and re-derives the locked/
    unlocked status. This means a system that was previously unlocked
    will re-lock instantly if any condition degrades.
    """

    def __init__(self, conditions: Optional[dict] = None):
        self.conditions = conditions or UNLOCK_CONDITIONS
        log.info(
            "LiveGate init: %d conditions — %s",
            len(self.conditions), list(self.conditions.keys()),
        )

    def evaluate(
        self,
        oracle_score: Optional[dict] = None,
        analytics_report: Optional[dict] = None,
        shadow_report: Optional[dict] = None,
        exec_self_test: Optional[dict] = None,
    ) -> GateStatus:
        """Run all 6 unlock checks. Returns a GateStatus.

        Args:
            oracle_score: symbol-scoped authoritative score (must have
                          ``authority_1h`` from build_authoritative_score)
            analytics_report: dict from PortfolioAnalytics.compute()
                              (must have 'profit_factor', 'max_drawdown_pct')
            shadow_report: dict from ShadowLive.generate_report()
                          (must have 'passed')
            exec_self_test: dict from ExecutionEngine.self_test()
                           (must have 'verified')

        Returns:
            GateStatus with unlocked=True only if ALL conditions pass.
        """
        oracle_score = oracle_score or {}
        analytics_report = analytics_report or {}
        shadow_report = shadow_report or {}
        exec_self_test = exec_self_test or {}

        # Fail closed unless the score exposes the explicit independent cohort.
        # Raw ``by_window`` and top-level compatibility fields are diagnostic
        # only and can never authorize readiness.
        authority_1h = oracle_score.get("authority_1h") or {}
        global_1h = authority_1h.get("global") or {}
        valid_authority = bool(
            oracle_score.get("score_scope") == "PER_SYMBOL"
            and oracle_score.get("requested_symbol")
            and authority_1h.get("n_source") == "INDEPENDENT_NONOVERLAP_1H"
            and global_1h.get("n_source") == "INDEPENDENT_NONOVERLAP_1H"
        )
        global_win_rate = float(global_1h.get("win_rate_pct") or 0) if valid_authority else 0.0
        verified = int(global_1h.get("verified") or 0) if valid_authority else 0

        profit_factor = float(analytics_report.get("profit_factor") or 0)
        # Handle inf PF (all wins, no losses) — convert to large finite number
        if profit_factor == float("inf"):
            profit_factor = 1e6
        max_drawdown_pct = float(analytics_report.get("max_drawdown_pct") or 0)

        shadow_passed = bool(shadow_report.get("passed") or False)
        exec_verified = bool(exec_self_test.get("verified") or False)

        values = {
            "global_win_rate_pct": global_win_rate,
            "verified": verified,
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_drawdown_pct,
            "shadow_live_passed": shadow_passed,
            "execution_engine_verified": exec_verified,
        }

        # Evaluate each condition
        conditions_result: dict[str, dict] = {}
        failed: list[str] = []
        for name, spec in self.conditions.items():
            actual = values.get(name)
            threshold = spec["value"]
            op = spec["op"]
            passed = self._compare(actual, op, threshold)
            conditions_result[name] = {
                "value": actual,
                "threshold": threshold,
                "op": op,
                "pass": passed,
            }
            if not passed:
                failed.append(
                    f"{name}: {actual} {op} {threshold} → FAIL"
                )

        unlocked = len(failed) == 0
        status = GateStatus(
            unlocked=unlocked,
            trade_mode="LIVE" if unlocked else "PAPER",
            live_capital_locked=not unlocked,
            conditions=conditions_result,
            failed_reasons=failed,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )
        if unlocked:
            log.warning("LIVE_GATE UNLOCKED — all 6 conditions pass")
        else:
            log.info(
                "LIVE_GATE LOCKED — %d/%d conditions pass, failures: %s",
                len(self.conditions) - len(failed), len(self.conditions),
                "; ".join(failed) if failed else "none",
            )
        return status

    @staticmethod
    def _compare(actual: Any, op: str, threshold: Any) -> bool:
        try:
            if op == ">=":
                return actual >= threshold
            if op == ">":
                return actual > threshold
            if op == "<=":
                return actual <= threshold
            if op == "<":
                return actual < threshold
            if op == "==":
                return actual == threshold
            if op == "!=":
                return actual != threshold
        except Exception:
            return False
        return False

    def get_state(self) -> dict[str, Any]:
        """Static info about the gate configuration."""
        return {
            "conditions": self.conditions,
            "version": "ACT-XXV-HEDGE-FUND-TRANSITION",
        }
