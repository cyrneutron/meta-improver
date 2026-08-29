"""Phase 2A normalized signal input contracts."""

from .service import IngestionService
from .models import (
    CIRunSignal,
    IssueSignal,
    LocalLogSignal,
    SignalEvent,
    SignalSource,
    normalize_ci_run,
    normalize_issue,
    normalize_local_log,
)
__all__ = [
    "IngestionService",
    "CIRunSignal",
    "IssueSignal",
    "LocalLogSignal",
    "SignalEvent",
    "SignalSource",
    "normalize_ci_run",
    "normalize_issue",
    "normalize_local_log",
]
