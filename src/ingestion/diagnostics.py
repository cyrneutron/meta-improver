"""Deterministic, read-only summaries of canonical Harness Anything context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from src.ingestion.ha_adapter import (
    HAAdapterError,
    HADecision,
    HAFact,
    HAExecution,
    HAProgressEntry,
    HATaskContext,
    read_decision_context,
    read_fact_context,
    read_ha_context,
    read_task_context,
)
from src.ingestion.models import SignalEvent
from src.models import Attempt
from src.models.contracts import ContractModel, MAX_TEXT, redact


class DiagnosticError(HAAdapterError):
    """Raised when a complete canonical context cannot be read safely."""


class HADiagnosticCounts(ContractModel):
    """Counts of the canonical documents represented by a report."""

    harness_documents: int = Field(ge=0)
    task_documents: int = Field(ge=0)
    executions: int = Field(ge=0)
    progress_entries: int = Field(ge=0)
    progress_evidence: int = Field(ge=0)
    facts: int = Field(ge=0)
    decisions: int = Field(ge=0)


class HADiagnosticTask(ContractModel):
    """Task lifecycle and evidence summary."""

    task_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    status: str = Field(min_length=1, max_length=100)
    package_path: str = Field(min_length=1, max_length=500)
    executions: list[HAExecution] = Field(default_factory=list, max_length=100)
    progress_entries: list[HAProgressEntry] = Field(default_factory=list, max_length=1_000)


class HADiagnosticFact(ContractModel):
    """A fact snapshot with its canonical source reference."""

    fact_id: str = Field(min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=MAX_TEXT)
    evidence_source: str = Field(min_length=1, max_length=MAX_TEXT)
    observed_at: str = Field(min_length=1, max_length=100)
    confidence: str = Field(min_length=1, max_length=20)
    state: str = Field(min_length=1, max_length=50)
    source_ref: str = Field(min_length=1, max_length=500)


class HADiagnosticDecision(ContractModel):
    """A decision summary retaining bounded structured choices and relations."""

    decision_id: str = Field(min_length=1, max_length=200)
    state: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=500)
    question: str = Field(min_length=1, max_length=MAX_TEXT)
    chosen: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    rejected: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    claims: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    relations: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    source_ref: str = Field(min_length=1, max_length=500)


class HADiagnosticReport(ContractModel):
    """The complete bounded diagnostic context for one canonical task."""

    schema_: str = Field(default="diagnostic-summary/v1", alias="schema", serialization_alias="schema")
    task: HADiagnosticTask
    counts: HADiagnosticCounts
    facts: list[HADiagnosticFact] = Field(default_factory=list, max_length=500)
    decisions: list[HADiagnosticDecision] = Field(default_factory=list, max_length=100)
    source_refs: list[str] = Field(default_factory=list, max_length=2_000)
    source: str | None = Field(default=None, max_length=50)
    event_signature: str | None = Field(default=None, max_length=100)
    attempt_id: str | None = Field(default=None, max_length=100)
    attempt_status: str | None = Field(default=None, max_length=50)
    base_commit: str | None = Field(default=None, max_length=64)
    strategy_version: str | None = Field(default=None, max_length=100)

    def to_json(self) -> str:
        """Serialize using Pydantic's stable JSON representation."""
        return self.model_dump_json(by_alias=True)


def _task_summary(context: HATaskContext) -> HADiagnosticTask:
    return HADiagnosticTask(
        task_id=context.task_id,
        title=context.title,
        status=context.status,
        package_path=context.package_path,
        executions=context.executions,
        progress_entries=context.progress_entries,
    )


def _validate_evidence_reference(reference: str) -> None:
    if not isinstance(reference, str) or not reference.strip() or "\\" in reference:
        raise DiagnosticError("progress evidence reference is unsafe")
    parts = reference.split(":", 2)
    if len(parts) != 3 or not parts[0].strip() or not parts[1].strip() or not parts[2].strip():
        raise DiagnosticError("progress evidence reference must be type:path:summary")
    path = parts[1].strip()
    if path.startswith("/") or any(segment in {"", ".", ".."} for segment in path.split("/")):
        raise DiagnosticError("progress evidence path is unsafe")


def _fact_summary(fact: HAFact) -> HADiagnosticFact:
    return HADiagnosticFact(
        fact_id=fact.fact_id,
        statement=fact.statement,
        evidence_source=fact.evidence_source,
        observed_at=fact.observed_at,
        confidence=fact.confidence,
        state=fact.state,
        source_ref=f"harness/facts/{fact.fact_id}.md",
    )


def _decision_summary(decision: HADecision) -> HADiagnosticDecision:
    return HADiagnosticDecision(
        decision_id=decision.decision_id,
        state=decision.state,
        title=decision.title,
        question=decision.question,
        chosen=decision.chosen,
        rejected=decision.rejected,
        claims=decision.claims,
        relations=decision.relations,
        source_ref=f"harness/decisions/decision-{decision.decision_id}/decision.md",
    )


def _association_fields(
    signal_event: SignalEvent | None,
    attempt: Attempt | None,
) -> dict[str, str | None]:
    if signal_event is None and attempt is None:
        return {
            "source": None,
            "event_signature": None,
            "attempt_id": None,
            "attempt_status": None,
            "base_commit": None,
            "strategy_version": None,
        }
    event_signature = signal_event.signature if signal_event is not None else None
    attempt_signature = attempt.signal if attempt is not None else None
    if event_signature is not None and attempt_signature is not None and event_signature != attempt_signature:
        raise DiagnosticError("signal event and attempt signatures do not match")
    signature = event_signature or attempt_signature
    source = signal_event.source.value if signal_event is not None else attempt.input_snapshot.source
    return {
        "source": source,
        "event_signature": signature,
        "attempt_id": attempt.attempt_id if attempt is not None else None,
        "attempt_status": attempt.status.value if attempt is not None else None,
        "base_commit": attempt.base_commit if attempt is not None else None,
        "strategy_version": attempt.strategy_version if attempt is not None else None,
    }


def build_diagnostic_summary(
    repo_root: str | Path,
    task_id: str,
    *,
    max_text: int = MAX_TEXT,
    signal_event: SignalEvent | None = None,
    attempt: Attempt | None = None,
) -> HADiagnosticReport:
    """Build a deterministic report from canonical readers without side effects."""
    try:
        _harness = read_ha_context(repo_root, max_text=max_text)
        task = read_task_context(repo_root, task_id, max_text=max_text)
        facts = read_fact_context(repo_root, max_text=max_text)
        decisions = read_decision_context(repo_root, max_text=max_text)
    except HAAdapterError as exc:
        raise DiagnosticError(f"canonical diagnostic context is unavailable: {exc}") from exc

    task_summary = _task_summary(task)
    progress_evidence = [reference for entry in task_summary.progress_entries for reference in entry.evidence]
    for reference in progress_evidence:
        _validate_evidence_reference(reference)
    fact_summaries = [_fact_summary(fact) for fact in facts]
    decision_summaries = [_decision_summary(decision) for decision in decisions]
    association = _association_fields(signal_event, attempt)
    source_refs = [
        "harness/harness.yaml",
        "harness/people.yaml",
        f"harness/{task.package_path}/INDEX.md",
        f"harness/{task.package_path}/task-contract.json",
        f"harness/{task.package_path}/progress.md",
        *(
            f"harness/{task.package_path}/executions/{execution.execution_id}.md"
            for execution in task.executions
        ),
        *progress_evidence,
        *(fact.source_ref for fact in fact_summaries),
        *(decision.source_ref for decision in decision_summaries),
    ]
    return HADiagnosticReport(
        task=redact(task_summary),
        counts=HADiagnosticCounts(
            harness_documents=2,
            task_documents=3 + len(task.executions),
            executions=len(task.executions),
            progress_entries=len(task.progress_entries),
            progress_evidence=len(progress_evidence),
            facts=len(fact_summaries),
            decisions=len(decision_summaries),
        ),
        facts=redact(fact_summaries),
        decisions=redact(decision_summaries),
        source_refs=source_refs,
        **association,
    )


read_diagnostic_summary = build_diagnostic_summary


__all__ = [
    "DiagnosticError",
    "HADiagnosticCounts",
    "HADiagnosticDecision",
    "HADiagnosticFact",
    "HADiagnosticReport",
    "HADiagnosticTask",
    "build_diagnostic_summary",
    "read_diagnostic_summary",
]
