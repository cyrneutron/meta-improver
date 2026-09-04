"""Bounded, read-only Squad diagnosis and ledger record contract.

The coordinator consumes only the controlled HA adapter.  It never applies a
patch, starts a container, or publishes to GitHub; its output is a durable
diagnostic fact that later validation stages can reference.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.ha_cli import HaCliAdapter, HaCliStatus, HaCliStatusPollReceipt
from src.models import Attempt, AttemptStatus, InputSnapshot


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SECRET = re.compile(r"(?:bearer\s+\S+|(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+)", re.I)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _safe_text(value: str, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or _SECRET.search(value):
        raise ValueError(f"{field_name} contains unsafe or secret-shaped text")
    return value


class HaDiagnosisError(ValueError):
    """Raised when a provider diagnosis cannot be trusted."""


class HaDiagnosisStatus(StrEnum):
    CONVERGED = "converged"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"


class HaDiagnosisFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    path: str = Field(min_length=1, max_length=500)
    observation: str = Field(min_length=1, max_length=4_000)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if value.startswith(("/", "~")) or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("diagnosis finding path must be a safe relative POSIX path")
        return value

    @field_validator("observation")
    @classmethod
    def safe_observation(cls, value: str) -> str:
        return _safe_text(value, "observation", 4_000)


class HaDiagnosis(BaseModel):
    """A hash-bound, provider-independent read-only diagnosis."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_: Literal["ha-diagnosis/v1"] = Field(default="ha-diagnosis/v1", alias="schema", serialization_alias="schema")
    diagnosis_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)
    squad_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    status: HaDiagnosisStatus
    summary: str | None = Field(default=None, max_length=4_000)
    findings: list[HaDiagnosisFinding] = Field(default_factory=list, max_length=32)
    provider_version: str = Field(min_length=1, max_length=100)
    provider_build_id: str = Field(min_length=1, max_length=200)
    poll_attempts: int = Field(ge=0, le=1_000)
    receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=2_000)
    observed_at: datetime
    record_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("diagnosis_id", "task_id", "squad_id", "run_id")
    @classmethod
    def identifiers(cls, value: str, info: Any) -> str:
        return _safe_identifier(value, info.field_name)

    @field_validator("observed_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("summary")
    @classmethod
    def safe_summary(cls, value: str | None) -> str | None:
        return None if value is None else _safe_text(value, "summary", 4_000)

    @field_validator("provider_version", "provider_build_id")
    @classmethod
    def safe_provider_identity(cls, value: str, info: Any) -> str:
        return _safe_text(value, info.field_name, 200)

    @model_validator(mode="after")
    def validate_state_and_hash(self) -> HaDiagnosis:
        if self.status is HaDiagnosisStatus.CONVERGED and self.summary is None and not self.findings:
            raise ValueError("a converged diagnosis requires a summary or finding")
        if self.status is not HaDiagnosisStatus.CONVERGED and not self.reason:
            raise ValueError("a rejected diagnosis requires a reason")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"record_hash"}))
        if self.record_hash is not None and self.record_hash != expected:
            raise ValueError("diagnosis record_hash does not match canonical contents")
        object.__setattr__(self, "record_hash", expected)
        return self


def _nested_mapping(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _run_id(payload: dict[str, Any]) -> str | None:
    for key in ("runId", "squadRunId", "run_id", "squad_run_id"):
        value = payload.get(key)
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            return value
    for key in ("detail", "squadRun", "run"):
        nested = _nested_mapping(payload.get(key))
        if nested:
            found = _run_id(nested)
            if found:
                return found
    return None


def _decision(payload: dict[str, Any]) -> dict[str, Any] | None:
    value = payload.get("decision")
    if isinstance(value, dict):
        # HA status receipts keep the leader's final synthesis in ``report``
        # rather than duplicating its structured fields. Preserve the report
        # as a bounded summary; do not infer findings from Markdown.
        if value.get("kind") == "converged" and not value.get("summary"):
            report = value.get("report")
            if isinstance(report, str) and report.strip():
                return {**value, "summary": report}
        return value
    for key in ("detail", "squadRun", "run"):
        nested = _nested_mapping(payload.get(key))
        if nested:
            found = _decision(nested)
            if found:
                return found
    leaders = payload.get("leaders")
    if isinstance(leaders, list):
        for leader in reversed(leaders):
            nested = _nested_mapping(leader)
            if nested:
                found = _decision(nested)
                if found:
                    return found
    return None


def collect_squad_diagnosis(
    adapter: HaCliAdapter,
    root,
    *,
    diagnosis_id: str,
    squad_id: str,
    instance: str,
    cwd: str,
    task_id: str,
    prompt: str,
    interval_seconds: float = 1.0,
    max_attempts: int = 30,
    deadline_seconds: float = 30.0,
) -> HaDiagnosis:
    """Run one bounded Squad Leader diagnosis and normalize its terminal result."""

    started = datetime.now(timezone.utc)
    launch = adapter.squad_run(root, squad_id, instance, cwd, task_id, prompt)
    launch_payload = launch.receipt or {}
    run_id = _run_id(launch_payload)
    if launch.status is HaCliStatus.UNSUPPORTED:
        return HaDiagnosis(
            diagnosis_id=diagnosis_id, task_id=task_id, squad_id=squad_id, run_id="unsupported",
            status=HaDiagnosisStatus.UNSUPPORTED, provider_version=launch.provider_version,
            provider_build_id=launch.provider_build_id, poll_attempts=0,
            receipt_digest=_digest(launch_payload), reason=launch.reason or "Squad run is unsupported.", observed_at=started,
        )
    if launch.status is not HaCliStatus.SUCCEEDED or not run_id:
        return HaDiagnosis(
            diagnosis_id=diagnosis_id, task_id=task_id, squad_id=squad_id, run_id=run_id or "rejected",
            status=HaDiagnosisStatus.REJECTED, provider_version=launch.provider_version,
            provider_build_id=launch.provider_build_id, poll_attempts=0,
            receipt_digest=_digest(launch_payload), reason=launch.reason or "Squad run was rejected or did not return a run id.", observed_at=started,
        )
    poll = adapter.poll_squad_status(
        root, run_id, interval_seconds=interval_seconds, max_attempts=max_attempts, deadline_seconds=deadline_seconds
    )
    return normalize_squad_status(
        poll,
        diagnosis_id=diagnosis_id,
        squad_id=squad_id,
        task_id=task_id,
        run_id=run_id,
        observed_at=started,
    )


def normalize_squad_status(
    poll: HaCliStatusPollReceipt,
    *,
    diagnosis_id: str,
    squad_id: str,
    task_id: str,
    run_id: str,
    observed_at: datetime | None = None,
) -> HaDiagnosis:
    """Normalize a previously polled terminal status without rerunning it."""

    payload = poll.receipt or {}
    decision = _decision(payload)
    summary = decision.get("summary") if decision else None
    raw_findings = decision.get("findings") if decision else None
    findings = [HaDiagnosisFinding.model_validate(item) for item in raw_findings] if isinstance(raw_findings, list) else []
    converged = (
        poll.status is HaCliStatus.SUCCEEDED
        and poll.terminal
        and isinstance(decision, dict)
        and decision.get("kind") == "converged"
    )
    return HaDiagnosis(
        diagnosis_id=diagnosis_id, task_id=task_id, squad_id=squad_id, run_id=run_id,
        status=HaDiagnosisStatus.CONVERGED if converged else HaDiagnosisStatus.REJECTED,
        summary=summary if isinstance(summary, str) else None, findings=findings,
        provider_version=poll.provider_version, provider_build_id=poll.provider_build_id,
        poll_attempts=poll.attempts, receipt_digest=_digest(payload),
        reason="" if converged else (poll.reason or "Squad run did not converge."), observed_at=observed_at or datetime.now(timezone.utc),
    )


def rehydrate_diagnosis(value: HaDiagnosis) -> HaDiagnosis:
    if not isinstance(value, HaDiagnosis) or value.record_hash is None:
        raise HaDiagnosisError("diagnosis record is not hashed")
    canonical = _canonical(value.model_dump(mode="json", by_alias=True))
    try:
        hydrated = HaDiagnosis.model_validate(json.loads(canonical))
    except Exception as exc:
        raise HaDiagnosisError("diagnosis record failed integrity rehydration") from exc
    if hydrated.record_hash != value.record_hash or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise HaDiagnosisError("diagnosis record hash does not match canonical contents")
    return hydrated


class DiagnosisAttemptLedger(Protocol):
    def record_diagnosis(self, diagnosis: HaDiagnosis) -> HaDiagnosis: ...

    def record_attempt(self, attempt: Attempt) -> Attempt: ...

    def transition_attempt(self, attempt: Attempt) -> Attempt: ...


def record_squad_diagnosis_attempt(
    ledger: DiagnosisAttemptLedger,
    diagnosis: HaDiagnosis,
    *,
    base_commit: str,
    strategy_version: str,
    model_version: str,
    prompt_version: str,
) -> Attempt:
    """Persist a diagnosis and create its captured, replay-safe Attempt handoff."""

    recorded_diagnosis = ledger.record_diagnosis(rehydrate_diagnosis(diagnosis))
    diagnosis_hash = recorded_diagnosis.record_hash or ""
    key = _digest(
        {
            "signal": diagnosis_hash,
            "base_commit": base_commit,
            "strategy_version": strategy_version,
        }
    )
    snapshot = InputSnapshot(
        source="manual",
        content=diagnosis_hash,
        content_sha256=hashlib.sha256(diagnosis_hash.encode("utf-8")).hexdigest(),
        captured_at=recorded_diagnosis.observed_at,
        metadata={
            "diagnosis_id": recorded_diagnosis.diagnosis_id,
            "task_id": recorded_diagnosis.task_id,
            "squad_id": recorded_diagnosis.squad_id,
            "run_id": recorded_diagnosis.run_id,
            "provider_version": recorded_diagnosis.provider_version,
            "provider_build_id": recorded_diagnosis.provider_build_id,
        },
    )
    initial = Attempt(
        attempt_id="attempt-" + key.removeprefix("sha256:"),
        idempotency_key=key,
        signal=diagnosis_hash,
        base_commit=base_commit,
        strategy_version=strategy_version,
        input_snapshot=snapshot,
        source_diagnosis_id=recorded_diagnosis.diagnosis_id,
        source_diagnosis_hash=diagnosis_hash,
        model_version=model_version,
        prompt_version=prompt_version,
        created_at=recorded_diagnosis.observed_at,
        updated_at=recorded_diagnosis.observed_at,
    )
    recorded_attempt = ledger.record_attempt(initial)
    if recorded_diagnosis.status is HaDiagnosisStatus.CONVERGED:
        return recorded_attempt
    rejected = Attempt.model_validate(
        {
            **recorded_attempt.model_dump(),
            "status": AttemptStatus.REJECTED,
            "failure_reason": recorded_diagnosis.reason,
            "updated_at": recorded_diagnosis.observed_at,
        }
    )
    return ledger.transition_attempt(rejected)


__all__ = [
    "HaDiagnosis", "HaDiagnosisError", "HaDiagnosisFinding", "HaDiagnosisStatus",
    "collect_squad_diagnosis", "normalize_squad_status", "record_squad_diagnosis_attempt",
    "rehydrate_diagnosis",
]
