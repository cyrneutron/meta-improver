"""Offline, hash-bound closeout records.

``CloseoutPacket`` is the last evidence boundary of an improvement attempt.
It records identities and digests only; it does not read Harness Anything,
invoke a provider, publish a change, or execute tests.  The deliberately flat
shape makes the record useful to both the local ledger and a later reviewer.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,499}$")
_CONTROL = re.compile(r"[;&|`$<>\r\n\x00]")
_SECRET = re.compile(
    r"(?:\bbearer\s+\S+|(?<![A-Za-z0-9])(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]+|"
    r"(?<![A-Za-z0-9])(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|authorization)"
    r"\s*[:=]\s*\S+)",
    re.IGNORECASE,
)

MAX_TEXT = 20_000
MAX_TESTS = 200
MAX_RISKS = 200


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_text(value: str, field_name: str, *, max_length: int = MAX_TEXT) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or _CONTROL.search(value)
        or _SECRET.search(value)
    ):
        raise ValueError(f"{field_name} contains unsafe or secret-shaped text")
    return value


def _safe_identifier(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not _IDENTIFIER.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith((".", "/", ":"))
        or _CONTROL.search(value)
        or _SECRET.search(value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _value_digest(value: Any, field_name: str) -> str:
    """Extract a known digest from a contract, mapping, or digest string."""

    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        # The order is intentional: receipt_hash/evidence_hash are the
        # identity of the supplied artifact, while nested hashes are merely
        # bindings inside that artifact.
        for key in (
            "receipt_hash",
            "evidence_hash",
            "observation_hash",
            "hypothesis_hash",
            "candidate_hash",
            "eval_hash",
            "plan_hash",
            "payload_hash",
            "token_hash",
            "signature",
            "digest",
            "hash",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    raise ValueError(f"{field_name} must contain a hash-bound artifact")


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return value if isinstance(value, Mapping) else None


def _nested_value(value: Any, *keys: str) -> Any:
    current: Any = value
    for key in keys:
        mapped = _mapping(current)
        if mapped is None or key not in mapped:
            return None
        current = mapped[key]
    return current


class CloseoutError(ValueError):
    """Raised when a closeout packet cannot be trusted or rehydrated."""


class _CloseoutContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class CloseoutPacket(_CloseoutContract):
    """A bounded, replayable index of all evidence required for closeout.

    Artifact fields are digests, not copied payloads.  They accept the
    corresponding existing Pydantic contracts as input and extract their
    already-derived digest, preserving old APIs and keeping this boundary
    execution-free.
    """

    schema_: Literal["closeout-packet/v1"] = Field(
        default="closeout-packet/v1", alias="schema", serialization_alias="schema"
    )
    signal: str = Field(
        min_length=1,
        max_length=500,
        validation_alias=AliasChoices("signal", "signal_hash", "signal_digest"),
    )
    baseline: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("baseline", "baseline_hash", "baseline_digest"),
    )
    holdout: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("holdout", "holdout_hash", "holdout_digest"),
    )
    candidate: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("candidate", "candidate_hash", "candidate_digest"),
    )
    pipeline_receipt: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("pipeline_receipt", "pipeline_receipt_hash", "pipeline_digest"),
    )
    acceptance_receipt: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices(
            "acceptance_receipt",
            "acceptance_receipt_hash",
            "acceptance_digest",
            "acceptance_receipt_digest",
        ),
    )
    task_id: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("task_id", "ha_task_id", "ha_task", "task"),
    )
    execution_id: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices(
            "execution_id", "ha_execution_id", "ha_execution", "execution"
        ),
    )
    provider: str = Field(
        min_length=1,
        max_length=500,
        validation_alias=AliasChoices(
            "provider", "provider_id", "provider_version", "provider_build_id"
        ),
    )
    base_commit: str = Field(
        pattern=_COMMIT,
        validation_alias=AliasChoices("base_commit", "base", "base_revision"),
    )
    patch: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("patch", "patch_hash", "patch_digest"),
    )
    tests: list[str] = Field(min_length=1, max_length=MAX_TESTS)
    risks: list[str] = Field(max_length=MAX_RISKS)
    rollback: str = Field(min_length=1, max_length=2_000)
    review: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("review", "review_hash", "review_digest"),
    )
    consent: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("consent", "consent_hash", "consent_digest", "token_hash"),
    )
    publication: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("publication", "publication_hash", "publication_digest"),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        validation_alias=AliasChoices(
            "created_at", "closed_at", "issued_at", "timestamp"
        ),
    )
    tests_digest: str | None = Field(
        default=None,
        pattern=_HASH,
        validation_alias=AliasChoices("tests_digest", "tests_hash"),
    )
    risks_digest: str | None = Field(
        default=None,
        pattern=_HASH,
        validation_alias=AliasChoices("risks_digest", "risks_hash"),
    )
    packet_hash: str | None = Field(
        default=None,
        pattern=_HASH,
        validation_alias=AliasChoices("packet_hash", "closeout_hash", "packet_digest", "digest"),
    )

    @field_validator(
        "baseline",
        "holdout",
        "candidate",
        "pipeline_receipt",
        "acceptance_receipt",
        "patch",
        "review",
        "consent",
        "publication",
        mode="before",
    )
    @classmethod
    def artifact_digest(cls, value: Any, info: Any) -> str:
        return _value_digest(value, info.field_name)

    @field_validator("signal", mode="before")
    @classmethod
    def safe_signal(cls, value: Any) -> str:
        if not isinstance(value, str):
            mapped = _mapping(value)
            if mapped is not None and isinstance(mapped.get("signature"), str):
                value = mapped["signature"]
        return _safe_text(value, "signal", max_length=500)

    @field_validator("task_id", "execution_id", mode="before")
    @classmethod
    def artifact_identity(cls, value: Any, info: Any) -> str:
        if not isinstance(value, str):
            mapped = _mapping(value)
            if mapped is not None:
                key = "task_id" if info.field_name == "task_id" else "execution_id"
                value = mapped.get(key)
        return value

    @field_validator("task_id", "execution_id", "provider")
    @classmethod
    def safe_identity(cls, value: str, info: Any) -> str:
        return _safe_identifier(value, info.field_name)

    @field_validator("tests", "risks")
    @classmethod
    def safe_entries(cls, values: list[str], info: Any) -> list[str]:
        return [_safe_text(value, info.field_name, max_length=MAX_TEXT) for value in values]

    @field_validator("rollback", mode="before")
    @classmethod
    def safe_rollback(cls, value: Any) -> str:
        if not isinstance(value, str):
            mapped = _mapping(value)
            if mapped is not None:
                value = mapped.get(
                    "rollback_ref", mapped.get("ref", mapped.get("commit"))
                )
        return _safe_text(value, "rollback", max_length=2_000)

    @field_validator("created_at")
    @classmethod
    def timestamp_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")

    @model_validator(mode="before")
    @classmethod
    def validate_receipt_bindings(cls, data: Any) -> Any:
        """Check cross-contract bindings before artifact objects are compacted."""

        if not isinstance(data, Mapping):
            return data
        acceptance = data.get("acceptance_receipt")
        if acceptance is None:
            acceptance = data.get(
                "acceptance_receipt_hash",
                data.get("acceptance_receipt_digest", data.get("acceptance_digest")),
            )
        pipeline = data.get("pipeline_receipt")
        if pipeline is None:
            pipeline = data.get("pipeline_receipt_hash", data.get("pipeline_digest"))
        acceptance_hash = _value_digest(acceptance, "acceptance_receipt") if acceptance is not None else None
        pipeline_map = _mapping(pipeline)
        if pipeline_map is not None and acceptance_hash is not None:
            bound = pipeline_map.get("acceptance_receipt_hash")
            if bound is not None and bound != acceptance_hash:
                raise ValueError("pipeline receipt is not bound to acceptance receipt")

        baseline = data.get("baseline", data.get("baseline_hash", data.get("baseline_digest")))
        candidate = data.get("candidate", data.get("candidate_hash", data.get("candidate_digest")))
        patch = data.get("patch", data.get("patch_hash", data.get("patch_digest")))
        acceptance_map = _mapping(acceptance)
        acceptance_plan = _nested_value(acceptance_map, "acceptance_plan")
        if acceptance_plan is None:
            acceptance_plan = _nested_value(acceptance_map, "plan")
        if acceptance_plan is not None:
            plan_baseline = _nested_value(acceptance_plan, "baseline", "observation_hash")
            plan_candidate = _nested_value(acceptance_plan, "candidate", "evidence_hash")
            plan_patch = _nested_value(acceptance_plan, "candidate", "patch_hash")
            if baseline is not None and plan_baseline is not None and _value_digest(baseline, "baseline") != plan_baseline:
                raise ValueError("acceptance receipt baseline binding does not match")
            if candidate is not None and plan_candidate is not None and _value_digest(candidate, "candidate") != plan_candidate:
                raise ValueError("acceptance receipt candidate binding does not match")
            if patch is not None and plan_patch is not None and _value_digest(patch, "patch") != plan_patch:
                raise ValueError("acceptance receipt patch binding does not match")
        return data

    @model_validator(mode="after")
    def derive_digests_and_hash(self) -> CloseoutPacket:
        expected_tests = _digest(self.tests)
        if self.tests_digest is not None and self.tests_digest != expected_tests:
            raise ValueError("tests_digest does not match canonical tests")
        object.__setattr__(self, "tests_digest", expected_tests)

        expected_risks = _digest(self.risks)
        if self.risks_digest is not None and self.risks_digest != expected_risks:
            raise ValueError("risks_digest does not match canonical risks")
        object.__setattr__(self, "risks_digest", expected_risks)

        expected_packet = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"packet_hash"})
        )
        if self.packet_hash is not None and self.packet_hash != expected_packet:
            raise ValueError("packet_hash does not match canonical contents")
        object.__setattr__(self, "packet_hash", expected_packet)
        return self

    @property
    def closeout_hash(self) -> str:
        """Compatibility spelling for consumers that call the packet a closeout."""

        return self.packet_hash or ""

    @property
    def digest(self) -> str:
        return self.packet_hash or ""

    @property
    def baseline_hash(self) -> str:
        return self.baseline

    @property
    def holdout_hash(self) -> str:
        return self.holdout

    @property
    def candidate_hash(self) -> str:
        return self.candidate

    @property
    def pipeline_receipt_hash(self) -> str:
        return self.pipeline_receipt

    @property
    def acceptance_receipt_hash(self) -> str:
        return self.acceptance_receipt

    @property
    def patch_hash(self) -> str:
        return self.patch


def rehydrate_closeout_packet(packet: CloseoutPacket) -> CloseoutPacket:
    """Round-trip a packet and reject mutation, stale digests, or extra data."""

    if not isinstance(packet, CloseoutPacket) or not packet.packet_hash:
        raise CloseoutError("cannot use an unhashed closeout packet")
    existing_hash = packet.packet_hash
    canonical = _canonical(packet.model_dump(mode="json", by_alias=True))
    try:
        hydrated = CloseoutPacket.model_validate(json.loads(canonical))
    except Exception as exc:
        raise CloseoutError("closeout packet failed integrity rehydration") from exc
    if (
        hydrated.packet_hash != existing_hash
        or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical
    ):
        raise CloseoutError("closeout packet hash does not match canonical contents")
    return hydrated


# Naming parallels the other protocol modules.
rehydrate_closeout = rehydrate_closeout_packet
rehydrate_packet = rehydrate_closeout_packet


__all__ = [
    "CloseoutError",
    "CloseoutPacket",
    "MAX_RISKS",
    "MAX_TESTS",
    "MAX_TEXT",
    "rehydrate_closeout",
    "rehydrate_closeout_packet",
    "rehydrate_packet",
]
