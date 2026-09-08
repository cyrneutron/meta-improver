"""Pydantic contracts used by Meta-Improver."""

from .contracts import (
    Attempt,
    AttemptStage,
    AttemptStatus,
    InputSnapshot,
    MutationProposal,
    PatchFile,
    ResidualRisk,
    TestEvidence,
)
from .report_chain import (
    ReportArtifact,
    ReportChain,
    ReportChainError,
    ReportEvidence,
    ReportFinding,
    append_report,
    rehydrate_report_artifact,
    rehydrate_report_chain,
)

__all__ = [
    "Attempt",
    "AttemptStage",
    "AttemptStatus",
    "InputSnapshot",
    "MutationProposal",
    "PatchFile",
    "ResidualRisk",
    "TestEvidence",
    "ReportArtifact",
    "ReportChain",
    "ReportChainError",
    "ReportEvidence",
    "ReportFinding",
    "append_report",
    "rehydrate_report_artifact",
    "rehydrate_report_chain",
]
