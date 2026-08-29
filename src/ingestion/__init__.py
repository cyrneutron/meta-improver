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
from .ha_adapter import (
    HAAdapterError,
    HAContext,
    HAExecution,
    HAProgressEntry,
    HATaskContext,
    read_ha_context,
    read_task_context,
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
    "HAAdapterError",
    "HAContext",
    "HAExecution",
    "HAProgressEntry",
    "HATaskContext",
    "read_ha_context",
    "read_task_context",
]
