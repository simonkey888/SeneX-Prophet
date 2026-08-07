"""SENEX Signal Lab + Live Terminal V1.

This package is intentionally read-only/paper-only.  It has no authenticated
trading client, signing material, wallet integration, or capital mutation
surface.
"""

from .contracts import EVENT_TYPES, FeatureValue, RawEvent
from .features import FeatureEngine
from .registry import ContradictionLedger, ExperimentRegistry
from .store import PointInTimeStore, RawAppendOnlyChain

__all__ = [
    "EVENT_TYPES",
    "FeatureValue",
    "RawEvent",
    "FeatureEngine",
    "ExperimentRegistry",
    "ContradictionLedger",
    "PointInTimeStore",
    "RawAppendOnlyChain",
]
