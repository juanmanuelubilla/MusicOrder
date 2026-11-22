"""Merge package: library fusion planning and execution."""

from musicfusion.merge.consolidator import (
    ConsolidationPlan,
    ConsolidationPlanner,
    VerificationResult,
)
from musicfusion.merge.engine import (
    CopiedItem,
    DuplicateItem,
    ErrorItem,
    FusionEngine,
    FusionOptions,
    FusionReport,
    ProgressCallback,
    ProgressData,
)

__all__ = [
    "ConsolidationPlan",
    "ConsolidationPlanner",
    "VerificationResult",
    "FusionEngine",
    "FusionOptions",
    "FusionReport",
    "ProgressData",
    "ProgressCallback",
    "CopiedItem",
    "DuplicateItem",
    "ErrorItem",
]
