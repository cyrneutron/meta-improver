"""Immutable, hash-bound report artifacts for sequential Squad review."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.models.contracts import redact


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_REPORT_REF = re.compile(r"^report/[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_CONTROL = re.compile(r"[\x00\r\n]")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _redact_input(value: Any) -> Any:
    if isinstance(value, dict):
        return redact({key: _redact_input(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_redact_input(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_input(item) for item in value)
    return redact(value)


def _safe_text(value: str, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length or _CONTROL.search(value):
        raise ValueError(f"{field_name} is invalid or unbounded")
    return value


class ReportChainError(ValueError):
    """Raised when an append-only report chain cannot be trusted."""


class ReportEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    ref: str = Field(min_length=1, max_length=500)
    kind: Literal["source", "test", "artifact", "decision", "report"]
    summary: str = Field(min_length=1, max_length=2_000)

    @field_validator("ref")
    @classmethod
    def safe_ref(cls, value: str) -> str:
        return _safe_text(value, "ref", max_length=500)

    @field_validator("summary")
    @classmethod
    def bounded_summary(cls, value: str) -> str:
        return _safe_text(value, "summary", max_length=2_000)


class ReportFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    claim_id: str = Field(min_length=1, max_length=200)
    assessment: str = Field(min_length=1, max_length=8_000)
    severity: Literal["low", "medium", "high", "critical"]
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=100)

    @field_validator("claim_id")
    @classmethod
    def safe_claim_id(cls, value: str) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("claim_id is invalid")
        return value

    @field_validator("assessment")
    @classmethod
    def bounded_assessment(cls, value: str) -> str:
        return _safe_text(value, "assessment", max_length=8_000)

    @field_validator("evidence_refs")
    @classmethod
    def bounded_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in values):
            raise ValueError("evidence_refs are invalid")
        return values


class ReportArtifact(BaseModel):
    """One immutable report owned by one worker attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_: Literal["squad-report/v1"] = Field(
        default="squad-report/v1", alias="schema", serialization_alias="schema"
    )
    report_id: str = Field(min_length=1, max_length=200)
    attempt_id: str = Field(min_length=1, max_length=200)
    reviewer_id: str = Field(min_length=1, max_length=200)
    reviewer_role: Literal["reviewer_a", "reviewer_b", "leader_synthesis"]
    round: int = Field(ge=1, le=100)
    prior_report_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    source_identity: str = Field(min_length=1, max_length=500)
    target_harness_identity: str = Field(min_length=1, max_length=500)
    model_version: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=8_000)
    findings: tuple[ReportFinding, ...] = Field(default_factory=tuple, max_length=100)
    challenged_claims: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    evidence: tuple[ReportEvidence, ...] = Field(default_factory=tuple, max_length=200)
    open_disagreements: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    report_hash: str | None = Field(default=None, pattern=_HASH.pattern)

    @model_validator(mode="before")
    @classmethod
    def redact_report_input(cls, value: Any) -> Any:
        return _redact_input(value)

    @field_validator("report_id", "attempt_id", "reviewer_id", "model_version", "prompt_version")
    @classmethod
    def safe_identifiers(cls, value: str, info: Any) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError(f"{info.field_name} is invalid")
        return value

    @field_validator("prior_report_refs")
    @classmethod
    def safe_report_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(_REPORT_REF.fullmatch(value) is None for value in values):
            raise ValueError("prior_report_refs must contain unique safe report references")
        return values

    @field_validator("source_identity", "target_harness_identity")
    @classmethod
    def safe_identities(cls, value: str, info: Any) -> str:
        return _safe_text(value, info.field_name, max_length=500)

    @field_validator("summary")
    @classmethod
    def bounded_report_summary(cls, value: str) -> str:
        return _safe_text(value, "summary", max_length=8_000)

    @field_validator("challenged_claims", "open_disagreements")
    @classmethod
    def bounded_text_items(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError(f"{info.field_name} must not contain duplicates")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 2_000 or _CONTROL.search(value) for value in values):
            raise ValueError(f"{info.field_name} contains invalid text")
        return values

    @model_validator(mode="after")
    def derive_report_hash(self) -> ReportArtifact:
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"report_hash"}))
        if self.report_hash is not None and self.report_hash != expected:
            raise ReportChainError("report_hash does not match canonical report contents")
        object.__setattr__(self, "report_hash", expected)
        return self


class ReportChain(BaseModel):
    """An immutable ordered sequence of reports with backward-only references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["squad-report-chain/v1"] = Field(
        default="squad-report-chain/v1", alias="schema", serialization_alias="schema"
    )
    reports: tuple[ReportArtifact, ...] = Field(default_factory=tuple, max_length=100)
    chain_hash: str | None = Field(default=None, pattern=_HASH.pattern)

    @model_validator(mode="after")
    def validate_chain(self) -> ReportChain:
        seen: dict[str, ReportArtifact] = {}
        for index, report in enumerate(self.reports):
            if report.report_id in seen:
                raise ReportChainError(f"duplicate report_id: {report.report_id}")
            if index == 0 and report.prior_report_refs:
                raise ReportChainError("the first report cannot reference a prior report")
            if index > 0 and not report.prior_report_refs:
                raise ReportChainError("every report after the first must reference prior reports")
            for reference in report.prior_report_refs:
                prior_id = reference.removeprefix("report/")
                if prior_id not in seen:
                    raise ReportChainError(f"report reference is missing or points forward: {reference}")
            if report.reviewer_role == "leader_synthesis" and index != len(self.reports) - 1:
                raise ReportChainError("leader synthesis must be the final report")
            seen[report.report_id] = report

        expected = _digest([report.report_hash for report in self.reports])
        if self.chain_hash is not None and self.chain_hash != expected:
            raise ReportChainError("chain_hash does not match report order")
        object.__setattr__(self, "chain_hash", expected)
        return self


def append_report(chain: ReportChain, report: ReportArtifact) -> ReportChain:
    """Return a new chain after validating one terminal report append."""
    try:
        report = rehydrate_report_artifact(report)
        return ReportChain(reports=chain.reports + (report,))
    except ReportChainError:
        raise
    except Exception as exc:
        raise ReportChainError("report could not be appended to chain") from exc


def rehydrate_report_artifact(report: ReportArtifact) -> ReportArtifact:
    if not isinstance(report, ReportArtifact) or report.report_hash is None:
        raise ReportChainError("cannot use an unhashed report artifact")
    try:
        hydrated = ReportArtifact.model_validate(
            json.loads(_canonical(report.model_dump(mode="json", by_alias=True)))
        )
    except ReportChainError:
        raise
    except Exception as exc:
        raise ReportChainError("report artifact failed integrity validation") from exc
    if hydrated.report_hash != report.report_hash:
        raise ReportChainError("report artifact hash does not match canonical contents")
    return hydrated


def rehydrate_report_chain(chain: ReportChain) -> ReportChain:
    if not isinstance(chain, ReportChain) or chain.chain_hash is None:
        raise ReportChainError("cannot use an unhashed report chain")
    try:
        hydrated = ReportChain.model_validate(
            json.loads(_canonical(chain.model_dump(mode="json", by_alias=True)))
        )
    except ReportChainError:
        raise
    except Exception as exc:
        raise ReportChainError("report chain failed integrity validation") from exc
    if hydrated.chain_hash != chain.chain_hash:
        raise ReportChainError("report chain hash does not match canonical order")
    return hydrated


__all__ = [
    "ReportArtifact",
    "ReportChain",
    "ReportChainError",
    "ReportEvidence",
    "ReportFinding",
    "append_report",
    "rehydrate_report_artifact",
    "rehydrate_report_chain",
]
