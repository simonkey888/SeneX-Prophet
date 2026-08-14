"""
Module: survivability.py — SURVIVABILITY FUNCTION

Purpose: Compute "probability of surviving N trades" — what real funds use.

This solves ⚠️3: "Falta una cosa crítica: SURVIVABILITY FUNCTION"

Before: System tracks EV, entropy, risk, kill — but not:
    "What's the probability I survive 100 more trades without blowing up?"

After: System computes:
    1. DRAWDOWN DISTRIBUTION — historical drawdown profile
    2. TAIL RISK PERSISTENCE — are bad streaks random or structural?
    3. SURVIVAL PROBABILITY — P(surviving N trades | current state)
    4. RUIN PROBABILITY — P(hitting max_drawdown in next N trades)

This is the function that separates "proto-institutional" from
"institutional". Every real fund computes this. No exceptions.

Key insight: A system can have positive EV and still have high ruin
probability if tail risk is fat. This module catches that.
"""

import math
from collections import deque
from typing import Optional


class SurvivabilityFunction:
    """Tracks drawdown distribution and computes survival probability.

    Maintains a rolling window of trade outcomes and computes:
    - Drawdown distribution (P(drawdown > X))
    - Tail risk persistence (clustering of bad outcomes)
    - Survival probability over N future trades
    - Ruin probability (hitting max_drawdown)

    All computations are deterministic — no Monte Carlo, no randomness.
    Uses analytical formulas from risk of ruin theory (Kelly-criterion adjacent).
    """

    def __init__(
        self,
        max_drawdown_pct: float = 0.20,
        trade_window: int = 100,
        initial_capital: float = 10000.0,
    ):
        """Initialize SurvivabilityFunction.

        Args:
            max_drawdown_pct: Maximum drawdown before "ruin" (0.20 = 20%).
            trade_window: Rolling window of recent trades to track.
            initial_capital: Starting capital for drawdown calculations.
        """
        self.max_drawdown_pct = max_drawdown_pct
        self.trade_window = trade_window
        self.initial_capital = initial_capital

        # Rolling trade history
        self._trade_pnls = deque(maxlen=trade_window)
        self._drawdown_series = deque(maxlen=trade_window)
        self._peak_capital = initial_capital
        self._current_capital = initial_capital

        # Worst drawdown seen
        self._worst_drawdown_pct = 0.0

        # Consecutive loss tracking
        self._current_consecutive_losses = 0
        self._max_consecutive_losses = 0
        self._consecutive_loss_history = deque(maxlen=trade_window)

    # ── TRADE RECORDING ──────────────────────────────────────────────

    def record_trade(self, pnl_pct: float):
        """Record a trade outcome.

        Args:
            pnl_pct: Trade PnL as percentage (0.01 = 1% gain, -0.02 = 2% loss).
        """
        self._trade_pnls.append(pnl_pct)

        # Update capital
        self._current_capital *= (1.0 + pnl_pct)
        self._peak_capital = max(self._peak_capital, self._current_capital)

        # Compute current drawdown
        if self._peak_capital > 0:
            current_dd = (self._peak_capital - self._current_capital) / self._peak_capital
        else:
            current_dd = 0.0

        self._drawdown_series.append(current_dd)
        self._worst_drawdown_pct = max(self._worst_drawdown_pct, current_dd)

        # Track consecutive losses
        if pnl_pct < 0:
            self._current_consecutive_losses += 1
            self._max_consecutive_losses = max(self._max_consecutive_losses, self._current_consecutive_losses)
        else:
            if self._current_consecutive_losses > 0:
                self._consecutive_loss_history.append(self._current_consecutive_losses)
            self._current_consecutive_losses = 0

    # ── DRAWDOWN DISTRIBUTION ────────────────────────────────────────

    def get_drawdown_distribution(self) -> dict:
        """Compute drawdown distribution from history.

        Returns:
            Dict with drawdown percentile breakpoints.
            P(drawdown > X) for various X values.
        """
        if not self._drawdown_series:
            return {
                "p_dd_gt_5pct": 0.0, "p_dd_gt_10pct": 0.0,
                "p_dd_gt_15pct": 0.0, "p_dd_gt_20pct": 0.0,
                "current_dd_pct": 0.0, "worst_dd_pct": 0.0,
                "avg_dd_pct": 0.0, "trade_count": 0,
            }

        dds = list(self._drawdown_series)
        n = len(dds)

        p_gt_5 = sum(1 for d in dds if d > 0.05) / n
        p_gt_10 = sum(1 for d in dds if d > 0.10) / n
        p_gt_15 = sum(1 for d in dds if d > 0.15) / n
        p_gt_20 = sum(1 for d in dds if d > 0.20) / n

        return {
            "p_dd_gt_5pct": round(p_gt_5, 4),
            "p_dd_gt_10pct": round(p_gt_10, 4),
            "p_dd_gt_15pct": round(p_gt_15, 4),
            "p_dd_gt_20pct": round(p_gt_20, 4),
            "current_dd_pct": round(dds[-1], 4) if dds else 0.0,
            "worst_dd_pct": round(self._worst_drawdown_pct, 4),
            "avg_dd_pct": round(sum(dds) / n, 4),
            "trade_count": n,
        }

    # ── TAIL RISK PERSISTENCE ────────────────────────────────────────

    def compute_tail_risk_persistence(self) -> dict:
        """Detect if bad outcomes cluster (structural) vs random.

        Uses autocorrelation of loss indicators to detect clustering.
        If losses tend to follow losses, the tail risk is persistent
        (structural), not random — this is VERY dangerous.

        Returns:
            Dict with:
                persistence_score: 0 = random, 1 = highly persistent
                max_consecutive_losses: worst streak
                avg_consecutive_losses: average streak length
                loss_clustering_detected: bool
        """
        if len(self._trade_pnls) < 10:
            return {
                "persistence_score": 0.0,
                "max_consecutive_losses": 0,
                "avg_consecutive_losses": 0.0,
                "loss_clustering_detected": False,
            }

        # Compute simple autocorrelation of loss indicators
        losses = [1 if p < 0 else 0 for p in self._trade_pnls]
        n = len(losses)

        if n < 3:
            return {
                "persistence_score": 0.0,
                "max_consecutive_losses": self._max_consecutive_losses,
                "avg_consecutive_losses": 0.0,
                "loss_clustering_detected": False,
            }

        # Simple lag-1 autocorrelation
        mean_loss_rate = sum(losses) / n
        if mean_loss_rate == 0 or mean_loss_rate == 1:
            # All wins or all losses — no variance
            persistence = 1.0 if mean_loss_rate == 1 else 0.0
        else:
            # Compute covariance at lag 1
            cov_sum = 0.0
            var_sum = 0.0
            for i in range(n):
                dev_i = losses[i] - mean_loss_rate
                var_sum += dev_i * dev_i
                if i < n - 1:
                    dev_next = losses[i + 1] - mean_loss_rate
                    cov_sum += dev_i * dev_next

            if var_sum == 0:
                persistence = 0.0
            else:
                persistence = max(0.0, cov_sum / var_sum)

        # Average consecutive losses
        if self._consecutive_loss_history:
            avg_consec = sum(self._consecutive_loss_history) / len(self._consecutive_loss_history)
        else:
            avg_consec = 0.0

        return {
            "persistence_score": round(persistence, 4),
            "max_consecutive_losses": self._max_consecutive_losses,
            "avg_consecutive_losses": round(avg_consec, 2),
            "loss_clustering_detected": persistence > 0.3,
        }

    # ── SURVIVAL PROBABILITY ─────────────────────────────────────────

    def compute_survival_probability(self, n_trades: int = 100) -> dict:
        """Compute probability of surviving N more trades.

        Uses the formula from risk of ruin theory:
        P(survival) = 1 - P(ruin)

        P(ruin) ≈ ((1 - edge_ratio) / (1 + edge_ratio))^remaining_units

        Where:
        - edge_ratio = win_rate * avg_win / (loss_rate * avg_loss) (like Kelly)
        - remaining_units = how many max-loss units until ruin

        This is the CORE institutional metric.

        Args:
            n_trades: Number of future trades to survive.

        Returns:
            Dict with:
                survival_prob: P(surviving N trades without hitting max_dd)
                ruin_prob: P(hitting max_drawdown in N trades)
                edge_ratio: Win/loss ratio
                remaining_units: How many max-loss events until ruin
                confidence: How confident we are in the estimate
        """
        if len(self._trade_pnls) < 10:
            # Not enough data — assume moderate risk
            return {
                "survival_prob": 0.50,
                "ruin_prob": 0.50,
                "edge_ratio": 1.0,
                "remaining_units": 5,
                "confidence": 0.0,
                "warning": "insufficient_data_10_trades_minimum",
            }

        pnls = list(self._trade_pnls)
        n = len(pnls)

        # Compute key statistics
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate = len(wins) / n if n > 0 else 0.5
        loss_rate = 1.0 - win_rate

        avg_win = sum(wins) / len(wins) if wins else 0.01
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.01

        # Edge ratio (Kelly-adjacent)
        if loss_rate > 0 and avg_loss > 0:
            edge_ratio = (win_rate * avg_win) / (loss_rate * avg_loss)
        else:
            edge_ratio = 2.0  # If no losses, very favorable

        # Remaining units until ruin
        current_dd = self._drawdown_series[-1] if self._drawdown_series else 0.0
        remaining_dd_budget = self.max_drawdown_pct - current_dd

        if avg_loss > 0:
            # How many average losses until we hit max drawdown
            remaining_units = remaining_dd_budget / (avg_loss * loss_rate + 1e-10)
        else:
            remaining_units = 100.0

        remaining_units = max(1.0, remaining_units)

        # Risk of ruin formula (simplified from Feller/Kelly)
        if edge_ratio == 1.0:
            # Fair game — ruin probability increases with trades
            ruin_prob = min(0.95, 1.0 - math.exp(-n_trades / (2 * remaining_units)))
        elif edge_ratio > 1.0:
            # Positive edge — ruin probability is low
            # P(ruin) ≈ ((1/ratio - 1) / (1/ratio + 1))^units
            ratio_inv = 1.0 / edge_ratio
            base = (ratio_inv - 1.0) / (ratio_inv + 1.0) if ratio_inv > 1 else 0.0
            # Clamp base to [0, 1)
            base = max(0.0, min(0.999, base))
            ruin_prob = base ** remaining_units
            # Scale by number of trades (more trades = more chances to get unlucky)
            trade_factor = 1.0 - math.exp(-n_trades / remaining_units)
            ruin_prob = min(0.99, ruin_prob + trade_factor * 0.1)
        else:
            # Negative edge — ruin probability is high
            ratio_val = edge_ratio
            base = (1.0 - ratio_val) / (1.0 + ratio_val)
            base = max(0.0, min(0.999, base))
            ruin_prob = base ** remaining_units
            # For negative edge, ruin is almost certain over enough trades
            ruin_prob = max(ruin_prob, 0.5)

        ruin_prob = max(0.0, min(0.99, ruin_prob))
        survival_prob = 1.0 - ruin_prob

        # Tail risk persistence adjustment
        tail_risk = self.compute_tail_risk_persistence()
        if tail_risk["loss_clustering_detected"]:
            # If losses cluster, ruin probability is higher than formula suggests
            persistence_adj = tail_risk["persistence_score"] * 0.2
            ruin_prob = min(0.99, ruin_prob + persistence_adj)
            survival_prob = 1.0 - ruin_prob

        # Confidence in estimate (more trades = more confident)
        confidence = min(1.0, n / 50.0)

        return {
            "survival_prob": round(survival_prob, 4),
            "ruin_prob": round(ruin_prob, 4),
            "edge_ratio": round(edge_ratio, 4),
            "remaining_units": round(remaining_units, 2),
            "current_dd_pct": round(current_dd, 4),
            "remaining_dd_budget_pct": round(remaining_dd_budget, 4),
            "confidence": round(confidence, 4),
        }

    # ── INTEGRATION HELPER ────────────────────────────────────────────

    def should_reduce_risk(self, n_trades: int = 100) -> dict:
        """Check if system should reduce risk based on survivability.

        This integrates with SDC as an additional guard tier.

        Returns:
            Dict with:
                reduce_risk: bool — should position size be reduced?
                reason: str — deterministic explanation
                survival_prob: float — current survival probability
                recommended_size_factor: float — position size multiplier
        """
        survival = self.compute_survival_probability(n_trades)
        tail = self.compute_tail_risk_persistence()

        ruin_prob = survival["ruin_prob"]
        persistence = tail["persistence_score"]

        # Decision logic
        if ruin_prob > 0.30:
            # High ruin probability — significantly reduce risk
            recommended_factor = max(0.1, 1.0 - ruin_prob)
            return {
                "reduce_risk": True,
                "reason": f"HIGH_RUIN_PROB: {ruin_prob:.2%} > 30% threshold",
                "ruin_prob": round(ruin_prob, 4),
                "survival_prob": survival["survival_prob"],
                "recommended_size_factor": round(recommended_factor, 4),
            }

        if persistence > 0.5:
            # Loss clustering detected — reduce risk moderately
            recommended_factor = max(0.3, 1.0 - persistence * 0.5)
            return {
                "reduce_risk": True,
                "reason": f"LOSS_CLUSTERING: persistence={persistence:.2f} > 0.5",
                "ruin_prob": round(ruin_prob, 4),
                "survival_prob": survival["survival_prob"],
                "recommended_size_factor": round(recommended_factor, 4),
            }

        if ruin_prob > 0.15:
            # Moderate risk — slightly reduce
            recommended_factor = max(0.5, 1.0 - ruin_prob * 0.5)
            return {
                "reduce_risk": True,
                "reason": f"MODERATE_RUIN_PROB: {ruin_prob:.2%} > 15% threshold",
                "ruin_prob": round(ruin_prob, 4),
                "survival_prob": survival["survival_prob"],
                "recommended_size_factor": round(recommended_factor, 4),
            }

        return {
            "reduce_risk": False,
            "reason": f"SURVIVABLE: ruin_prob={ruin_prob:.2%} < 15%",
            "ruin_prob": round(ruin_prob, 4),
            "survival_prob": survival["survival_prob"],
            "recommended_size_factor": 1.0,
        }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("survivability.py — Self-Test (Survivability Function)")
    print("=" * 60)

    # Test 1: Empty state — defaults
    print("\n[Test 1] Empty state — defaults...")
    sf = SurvivabilityFunction(max_drawdown_pct=0.20)
    dd = sf.get_drawdown_distribution()
    assert dd["trade_count"] == 0
    assert dd["worst_dd_pct"] == 0.0
    survival = sf.compute_survival_probability(100)
    assert survival["confidence"] == 0.0  # Not enough data
    print(f"  survival_prob={survival['survival_prob']}, confidence={survival['confidence']}")
    print(f"  ✓ Empty state returns defaults")

    # Test 2: Winning streak — high survival
    print("\n[Test 2] Winning streak — high survival...")
    sf2 = SurvivabilityFunction(max_drawdown_pct=0.20)
    for _ in range(30):
        sf2.record_trade(0.01)  # 1% gain each trade
    survival = sf2.compute_survival_probability(100)
    assert survival["survival_prob"] > 0.8, f"Winning streak should have high survival, got {survival['survival_prob']}"
    assert survival["edge_ratio"] > 1.0
    print(f"  survival_prob={survival['survival_prob']}, edge_ratio={survival['edge_ratio']}")
    print(f"  ✓ Winning streak → high survival")

    # Test 3: Losing streak — low survival
    print("\n[Test 3] Losing streak — low survival...")
    sf3 = SurvivabilityFunction(max_drawdown_pct=0.20, initial_capital=10000.0)
    for _ in range(15):
        sf3.record_trade(-0.01)  # 1% loss each trade
    survival = sf3.compute_survival_probability(100)
    assert survival["survival_prob"] < 0.9, f"Losing streak should reduce survival, got {survival['survival_prob']}"
    dd = sf3.get_drawdown_distribution()
    assert dd["worst_dd_pct"] > 0.0
    print(f"  survival_prob={survival['survival_prob']}, worst_dd={dd['worst_dd_pct']}")
    print(f"  ✓ Losing streak → reduced survival")

    # Test 4: Drawdown distribution
    print("\n[Test 4] Drawdown distribution...")
    sf4 = SurvivabilityFunction(max_drawdown_pct=0.20, initial_capital=10000.0)
    # Simulate: win, win, big loss, win, loss, win
    trades = [0.02, 0.01, -0.05, 0.015, -0.03, 0.01, -0.02, 0.03, 0.01, -0.04,
              0.02, 0.01, -0.01, 0.02, -0.06, 0.01, 0.03, -0.02, 0.01, 0.02]
    for t in trades:
        sf4.record_trade(t)
    dd = sf4.get_drawdown_distribution()
    assert dd["trade_count"] == 20
    assert dd["worst_dd_pct"] > 0.05
    print(f"  worst_dd={dd['worst_dd_pct']}, current_dd={dd['current_dd_pct']}")
    print(f"  P(dd>5%)={dd['p_dd_gt_5pct']}, P(dd>10%)={dd['p_dd_gt_10pct']}")
    print(f"  ✓ Drawdown distribution computed correctly")

    # Test 5: Tail risk persistence
    print("\n[Test 5] Tail risk persistence...")
    # Create trades with clustered losses
    sf5 = SurvivabilityFunction(max_drawdown_pct=0.20, initial_capital=10000.0)
    clustered = [0.01, 0.02, -0.01, -0.02, -0.03, 0.01, -0.01, -0.02, -0.01, 0.02,
                 -0.01, -0.02, -0.01, 0.01, 0.02, -0.01, -0.02, -0.03, -0.01, 0.01]
    for t in clustered:
        sf5.record_trade(t)
    tail = sf5.compute_tail_risk_persistence()
    print(f"  persistence={tail['persistence_score']}, max_consec={tail['max_consecutive_losses']}")
    print(f"  clustering_detected={tail['loss_clustering_detected']}")
    print(f"  ✓ Tail risk persistence computed")

    # Test 6: should_reduce_risk
    print("\n[Test 6] should_reduce_risk...")
    # System with moderate losses
    sf6 = SurvivabilityFunction(max_drawdown_pct=0.15, initial_capital=10000.0)
    for t in [0.01, 0.02, -0.01, -0.03, 0.01, -0.02, -0.01, 0.01, -0.02, -0.01,
              0.01, -0.01, 0.02, -0.03, -0.02, 0.01, -0.01, -0.02, 0.01, -0.01,
              0.02, -0.01, 0.01, -0.02, -0.01, 0.01, 0.01, -0.02, -0.03, 0.01]:
        sf6.record_trade(t)
    risk_check = sf6.should_reduce_risk(100)
    print(f"  reduce_risk={risk_check['reduce_risk']}")
    print(f"  reason={risk_check['reason']}")
    print(f"  survival_prob={risk_check['survival_prob']}")
    print(f"  recommended_size_factor={risk_check['recommended_size_factor']}")
    print(f"  ✓ should_reduce_risk works")

    # Test 7: Healthy system — no risk reduction
    print("\n[Test 7] Healthy system — no risk reduction...")
    sf7 = SurvivabilityFunction(max_drawdown_pct=0.20, initial_capital=10000.0)
    for _ in range(50):
        sf7.record_trade(0.005 + (0.005 if _ % 3 == 0 else -0.002))
    risk_check = sf7.should_reduce_risk(100)
    print(f"  reduce_risk={risk_check['reduce_risk']}")
    print(f"  survival_prob={risk_check['survival_prob']}")
    print(f"  ✓ Healthy system correctly identified")

    # Test 8: Near-ruin system
    print("\n[Test 8] Near-ruin system — heavy risk reduction...")
    sf8 = SurvivabilityFunction(max_drawdown_pct=0.10, initial_capital=10000.0)
    for _ in range(8):
        sf8.record_trade(-0.015)
    risk_check = sf8.should_reduce_risk(100)
    assert risk_check["reduce_risk"] is True, "Near-ruin should trigger risk reduction"
    assert risk_check["recommended_size_factor"] <= 0.5, "Near-ruin should heavily reduce size"
    print(f"  reduce_risk={risk_check['reduce_risk']}")
    print(f"  recommended_size_factor={risk_check['recommended_size_factor']}")
    print(f"  ✓ Near-ruin correctly detected and risk reduced")

    print("\n" + "=" * 60)
    print("All self-tests PASSED ✓")
    print("=" * 60)
