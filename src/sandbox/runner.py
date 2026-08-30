"""Pure protocol contracts for a future isolated container test runner.

This module deliberately contains no process, Docker, or Podman integration.  A
transport is an injected boundary; production integration can be added later
without weakening the request and result contracts exercised here.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Mapping, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from src.models.contracts import ContractModel, MAX_TEXT, redact
from src.sandbox.contracts import BoundedArgv, ContainerPolicy


class ContainerRunnerError(ValueError):
    """Base error for a request that cannot be admitted to the runner."""


class ContainerRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    RESOURCE_EXCEEDED = "resource_exceeded"
    REJECTED = "rejected"


def _safe_container_path(value: str) -> str:
    if not value.startswith("/") or value == "/" or "\\" in value or "\x00" in value:
        raise ValueError("workdir must be a non-root absolute container path")
    parts = value.split("/")[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("workdir contains an unsafe path segment")
    return value


class ContainerRunRequest(ContractModel):
    """A fully bounded request handed to an isolated transport."""

    security_fields = ("argv", "image", "workdir", "environment", "stdin")

    schema_: str = Field(default="container-run-request/v1", alias="schema", serialization_alias="schema")
    argv: BoundedArgv
    policy: ContainerPolicy = Field(default_factory=lambda: ContainerPolicy(timeout_seconds=60, memory_mb=512))
    image: str = Field(default="meta-improver-sandbox:fixture", min_length=1, max_length=200)
    workdir: str = Field(default="/workspace", max_length=4_096)
    environment: dict[str, str] = Field(default_factory=dict, max_length=32)
    stdin: str = Field(default="", max_length=MAX_TEXT)

    @field_validator("image")
    @classmethod
    def safe_image(cls, value: str) -> str:
        if any(character.isspace() for character in value) or any(
            character in value for character in ";&|`$<>\\\x00\r\n"
        ):
            raise ValueError("image must be one bounded image token")
        return value

    @field_validator("workdir")
    @classmethod
    def safe_workdir(cls, value: str) -> str:
        return _safe_container_path(value)

    @field_validator("environment")
    @classmethod
    def safe_environment(cls, value: dict[str, str]) -> dict[str, str]:
        # Environment is opt-in and never inherited from the host.  Secret-like
        # names or values are rejected rather than silently redacted.
        redacted = redact(value)
        if redacted != value:
            raise ValueError("container environment cannot contain credentials or secret-shaped values")
        for key, item in value.items():
            if not key or any(character in key for character in "=\\;|&`$<>\x00\r\n"):
                raise ValueError("environment names must be bounded tokens")
            if len(key) > 128 or len(item) > 1_000:
                raise ValueError("environment entries are too large")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def deny_policy_overrides(self) -> ContainerRunRequest:
        if self.policy.network_disabled is not True or self.policy.credentials is not False:
            raise ContainerRunnerError("container policy must disable network and credentials")
        return self

    @property
    def request_digest(self) -> str:
        payload = self.model_dump(mode="json", by_alias=True)
        return "sha256:" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class ContainerTransportReceipt(ContractModel):
    """The transport's bounded observation; it is not a Docker API object."""

    schema_: str = Field(default="container-transport-receipt/v1", alias="schema", serialization_alias="schema")
    exit_code: int | None = Field(default=0, ge=-255, le=255)
    stdout: str = Field(default="", max_length=MAX_TEXT)
    stderr: str = Field(default="", max_length=MAX_TEXT)
    duration_seconds: float = Field(default=0, ge=0, le=86_400)
    timed_out: bool = False
    peak_memory_mb: int | None = Field(default=None, ge=0, le=16_384)
    network_disabled: bool = True
    credentials: bool = False

    @model_validator(mode="after")
    def timeout_exit_shape(self) -> ContainerTransportReceipt:
        if self.timed_out and self.exit_code is not None:
            self.exit_code = None
        if not self.timed_out and self.exit_code is None:
            raise ValueError("a non-timeout transport receipt requires an exit code")
        return self


@runtime_checkable
class ContainerTransport(Protocol):
    """Injected execution boundary.  Implementations must not receive shell text."""

    def run(self, request: ContainerRunRequest) -> ContainerTransportReceipt:
        ...


class ContainerRunResult(ContractModel):
    """Stable, redacted evidence returned by :class:`ContainerRunner`."""

    schema_: str = Field(default="container-run-result/v1", alias="schema", serialization_alias="schema")
    status: ContainerRunStatus
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    stdout: str = Field(default="", max_length=MAX_TEXT)
    stderr: str = Field(default="", max_length=MAX_TEXT)
    duration_seconds: float = Field(default=0, ge=0, le=86_400)
    peak_memory_mb: int | None = Field(default=None, ge=0, le=16_384)
    reason: str = Field(default="", max_length=2_000)


class ContainerRunner:
    """Admit bounded requests and normalize an injected transport receipt."""

    def __init__(self, transport: ContainerTransport):
        if not isinstance(transport, ContainerTransport):
            raise TypeError("transport must implement ContainerTransport.run")
        self.transport = transport

    def run(self, request: ContainerRunRequest) -> ContainerRunResult:
        if not isinstance(request, ContainerRunRequest):
            raise ContainerRunnerError("runner accepts only ContainerRunRequest with BoundedArgv")
        # Rehydrate from canonical JSON immediately before crossing the
        # transport boundary.  Pydantic permits attribute assignment by
        # default, so this closes a mutation-after-validation escape hatch.
        try:
            request = ContainerRunRequest.model_validate(request.model_dump(mode="json", by_alias=True))
        except Exception as exc:
            raise ContainerRunnerError("container request changed after validation") from exc
        digest = request.request_digest
        try:
            raw_receipt = self.transport.run(request)
        except Exception:
            return ContainerRunResult(
                status=ContainerRunStatus.REJECTED,
                request_digest=digest,
                reason="transport failure",
            )
        try:
            if not isinstance(raw_receipt, ContainerTransportReceipt):
                raise TypeError("invalid receipt type")
            # A transport may return a mutable model that was changed after it
            # was validated.  Canonical rehydration re-applies all receipt
            # validators before any field is trusted below.
            receipt = ContainerTransportReceipt.model_validate(
                raw_receipt.model_dump(mode="json", by_alias=True)
            )
        except Exception:
            return ContainerRunResult(
                status=ContainerRunStatus.REJECTED,
                request_digest=digest,
                reason="invalid transport receipt",
            )
        common = {
            "request_digest": digest,
            "stdout": redact(receipt.stdout),
            "stderr": redact(receipt.stderr),
            "duration_seconds": receipt.duration_seconds,
            "peak_memory_mb": receipt.peak_memory_mb,
        }
        if receipt.network_disabled is not True or receipt.credentials is not False:
            return ContainerRunResult(
                status=ContainerRunStatus.REJECTED,
                request_digest=digest,
                reason="transport violated deny-by-default policy",
            )
        if receipt.peak_memory_mb is not None and receipt.peak_memory_mb > request.policy.memory_mb:
            return ContainerRunResult(status=ContainerRunStatus.RESOURCE_EXCEEDED, reason="memory limit exceeded", **common)
        if receipt.timed_out or receipt.duration_seconds > request.policy.timeout_seconds:
            return ContainerRunResult(status=ContainerRunStatus.TIMED_OUT, reason="timeout limit exceeded", **common)
        if receipt.exit_code == 0:
            return ContainerRunResult(status=ContainerRunStatus.SUCCEEDED, exit_code=0, **common)
        return ContainerRunResult(
            status=ContainerRunStatus.FAILED,
            exit_code=receipt.exit_code,
            reason="non-zero exit code",
            **common,
        )


class FakeContainerTransport:
    """Deterministic in-memory transport for protocol and fixture tests."""

    def __init__(
        self,
        fixtures: Mapping[str, ContainerTransportReceipt] | None = None,
        *,
        default: ContainerTransportReceipt | None = None,
    ) -> None:
        self.fixtures = dict(fixtures or {})
        self.default = default or ContainerTransportReceipt()
        self.requests: list[str] = []

    def run(self, request: ContainerRunRequest) -> ContainerTransportReceipt:
        digest = request.request_digest
        self.requests.append(digest)
        fixture = self.fixtures.get(digest, self.default)
        return fixture.model_copy(deep=True)


__all__ = [
    "ContainerRunRequest",
    "ContainerRunResult",
    "ContainerRunStatus",
    "ContainerRunner",
    "ContainerRunnerError",
    "ContainerTransport",
    "ContainerTransportReceipt",
    "FakeContainerTransport",
]
