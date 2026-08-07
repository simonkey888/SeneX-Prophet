"""SENEX external research pack V1.

Pure, paper-only capabilities extracted by independent reimplementation from
public protocol/methodology references. No wallet, signing or order authority.
"""

from .external_schema import normalize_public_event
from .fill_model import FillModelConfig, PaperFillRequest, simulate_paper_fill
from .replay_v2 import ReplayConfig, deterministic_replay
from .cross_market import CrossMarketState, DisabledCrossMarketAdapter

__all__ = [
    "normalize_public_event",
    "FillModelConfig",
    "PaperFillRequest",
    "simulate_paper_fill",
    "ReplayConfig",
    "deterministic_replay",
    "CrossMarketState",
    "DisabledCrossMarketAdapter",
]
