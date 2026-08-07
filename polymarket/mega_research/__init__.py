"""SENEX Mega Research Fusion V2 — isolated paper-only research composition."""
from .core import (
    AppendOnlyChain, EvidenceManifest, ExperimentConstitution, FAILURE_FIXTURES,
    FEATURE_FAMILY_MAP, FailureInjectionHarness, LeakageGate, MegaResearchFusion,
    ResearchLedger, ResearchPoint, SURFACES, StatisticalValidationEngine,
    SystemTruth, ValidationPolicy, render_read_only_terminal, terminal_projection,
    visual_contract_smoke,
)
__all__ = [name for name in globals() if not name.startswith("_")]
