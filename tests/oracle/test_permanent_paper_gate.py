from senecio_polymarket.backend.portfolio.live_gate import LiveGate


def test_even_perfect_inputs_cannot_unlock_live_capital():
    status = LiveGate().evaluate(
        oracle_score={
            "score_status": "CALIBRATED",
            "authoritative_score_pct": 80.0,
            "selected": {"verified": 1000},
        },
        analytics_report={"profit_factor": 5.0, "max_drawdown_pct": 1.0},
        shadow_report={"passed": True},
        exec_self_test={"verified": True},
    )
    assert status.unlocked is False
    assert status.trade_mode == "PAPER"
    assert status.live_capital_locked is True
    assert "PERMANENT_PAPER_ONLY_POLICY" in status.failed_reasons
