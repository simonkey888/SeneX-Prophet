"""SENEX paper-only simulated execution subsystem.

This package has no wallet, private-key, signing, authenticated trading, or
real-order authority.  It consumes public observations and emits replayable
simulation records only.
"""

from .models import (
    PaperDecision,
    PaperFill,
    PaperOrderIntent,
    PaperPortfolioSnapshot,
    PaperPosition,
    PaperRiskDecision,
    PaperTrialSummary,
)
from .broker import PublicOrderBook, SimulatedBroker
from .portfolio import PaperPortfolio
from .risk import PaperRiskConfig, PaperRiskEngine

__all__ = [
    "PaperDecision",
    "PaperFill",
    "PaperOrderIntent",
    "PaperPortfolioSnapshot",
    "PaperPosition",
    "PaperRiskDecision",
    "PaperTrialSummary",
    "PublicOrderBook",
    "SimulatedBroker",
    "PaperPortfolio",
    "PaperRiskConfig",
    "PaperRiskEngine",
]
