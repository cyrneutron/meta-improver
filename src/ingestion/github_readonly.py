"""Read-only GitHub transport and local fixture mappings.

No GitHub SDK is required.  The adapter talks to a small transport protocol so
all normal tests can use deterministic in-memory responses; the concrete
transport only permits the two GET endpoints needed by ingestion.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias

from pydantic import Field, ValidationError

from src.ingestion.models import CIRunSignal, IssueSignal
from src.models.contracts import MAX_TEXT


JSON: TypeAlias = Any

_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_READ_ENDPOINT_PATTERN = re.compile(
    r"^repos/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}/(?:actions/runs|issues)$"
)


class GitHubAdapterError(ValueError):
    """Raised when a transport boundary or GitHub fixture is invalid."""


class ReadonlyGitHubTransport(Protocol):
    """Transport seam receiving a complete, fixed read-only argv."""

    def run(self, argv: Sequence[str]) -> JSON:
        """Run one allowlisted ``gh api`` request and return decoded JSON."""


class GitHubCIRunSignal(CIRunSignal):
    """GitHub CI signal with optional output and GitHub's run statuses."""

    # The Actions runs list normally has no output.  Keep the empty value until
    # an explicit log source is joined by a later ingestion step.
    output: str = Field(default="", max_length=MAX_TEXT)
    conclusion: str = Field(default="", max_length=100)
    status: Literal["queued", "in_progress", "completed", "waiting", "requested", "pending"]


class GitHubIssueSignal(IssueSignal):
    """GitHub issue signal whose body may be absent or empty."""

    body: str = Field(default="", max_length=MAX_TEXT)


_ARGV_PREFIX = ("gh", "api", "--method", "GET", "--hostname", "github.com")


def _validate_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str) or not _READ_ENDPOINT_PATTERN.fullmatch(endpoint):
        raise GitHubAdapterError("only repository actions/runs and issues GET endpoints are allowed")


def _endpoint_from_argv(argv: Sequence[str]) -> str:
    values = tuple(argv)
    if len(values) != 9 or values[:6] != _ARGV_PREFIX or values[7:] != ("--input", "-"):
        raise GitHubAdapterError("transport argv is not the fixed read-only gh api command")
    endpoint = values[6]
    _validate_endpoint(endpoint)
    return endpoint


def _repository_endpoint(owner: str, repository: str, resource: str) -> str:
    if not isinstance(owner, str) or not _OWNER_PATTERN.fullmatch(owner):
        raise GitHubAdapterError("invalid GitHub owner")
    if not isinstance(repository, str) or not _REPOSITORY_PATTERN.fullmatch(repository):
        raise GitHubAdapterError("invalid GitHub repository")
    if resource not in {"actions/runs", "issues"}:
        raise GitHubAdapterError("unsupported GitHub read endpoint")
    endpoint = f"repos/{owner}/{repository}/{resource}"
    _validate_endpoint(endpoint)
    return endpoint


def _decode_stdout(stdout: str | bytes | bytearray | None, max_response_bytes: int) -> JSON:
    if isinstance(stdout, str):
        raw = stdout.encode("utf-8")
    elif isinstance(stdout, (bytes, bytearray)):
        raw = bytes(stdout)
    else:
        raise GitHubAdapterError("gh api returned no JSON output")
    if len(raw) > max_response_bytes:
        raise GitHubAdapterError(f"gh api response exceeds max_response_bytes={max_response_bytes}")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubAdapterError("gh api returned invalid JSON") from exc


class GhApiTransport:
    """Execute the fixed, read-only ``gh api`` command.

    ``gh`` may resolve authentication internally.  This class never reads,
    copies, logs, or includes credential values in an exception, and stderr is
    discarded so CLI diagnostics cannot expose them to callers.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if max_response_bytes < 1 or max_response_bytes > 20_000_000:
            raise ValueError("max_response_bytes must be between 1 and 20000000")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    def run(self, argv: Sequence[str]) -> JSON:
        """Run exactly one allowlisted argv, always with GET semantics."""
        _endpoint_from_argv(argv)
        values = tuple(argv)
        try:
            result = subprocess.run(
                values,
                check=False,
                input=b"",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitHubAdapterError("gh api transport failed") from exc
        if result.returncode != 0:
            raise GitHubAdapterError("gh api returned a non-zero status")
        return _decode_stdout(result.stdout, self.max_response_bytes)


class FixtureTransport:
    """Deterministic in-memory implementation of ``ReadonlyGitHubTransport``."""

    def __init__(self, responses: Mapping[str, JSON]) -> None:
        for endpoint in responses:
            _validate_endpoint(endpoint)
        self._responses = {endpoint: copy.deepcopy(response) for endpoint, response in responses.items()}
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> JSON:
        endpoint = _endpoint_from_argv(argv)
        values = tuple(argv)
        self.calls.append(values)
        if endpoint not in self._responses:
            raise GitHubAdapterError(f"no local fixture response for endpoint: {endpoint}")
        return copy.deepcopy(self._responses[endpoint])


def _required_string(payload: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value
    raise GitHubAdapterError(f"GitHub payload is missing {names[0]}")


def _optional_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise GitHubAdapterError(f"GitHub payload field is not a string: {name}")


def _identifier(payload: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, bool):
            raise GitHubAdapterError(f"GitHub payload identifier is invalid: {name}")
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    raise GitHubAdapterError(f"GitHub payload is missing {names[0]}")


def _timestamp(payload: Mapping[str, Any], *names: str) -> datetime:
    value = _required_string(payload, *names)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubAdapterError(f"GitHub payload timestamp is invalid: {names[0]}") from exc


def _commit_sha(payload: Mapping[str, Any]) -> str:
    direct = payload.get("head_sha")
    if isinstance(direct, str) and direct.strip():
        return direct
    head_commit = payload.get("head_commit")
    if isinstance(head_commit, Mapping):
        value = head_commit.get("id")
        if isinstance(value, str) and value.strip():
            return value
    raise GitHubAdapterError("GitHub run payload is missing head_sha")


def _build(model: type[GitHubCIRunSignal] | type[GitHubIssueSignal], payload: dict[str, Any]) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise GitHubAdapterError("GitHub payload does not match the local signal contract") from exc


def _map_ci_run(payload: Mapping[str, Any]) -> GitHubCIRunSignal:
    if not isinstance(payload, Mapping):
        raise GitHubAdapterError("GitHub run must be an object")
    raw_conclusion = payload.get("conclusion")
    if raw_conclusion is not None and not isinstance(raw_conclusion, str):
        raise GitHubAdapterError("GitHub run conclusion must be a string or null")
    raw_status = _required_string(payload, "status")
    return _build(
        GitHubCIRunSignal,
        {
            "run_id": _identifier(payload, "id", "run_id"),
            "workflow": _required_string(payload, "name", "workflow_name"),
            "status": raw_status,
            "conclusion": raw_conclusion or "",
            "commit_sha": _commit_sha(payload),
            "output": _optional_string(payload, "output"),
            "observed_at": _timestamp(payload, "updated_at", "created_at", "run_started_at"),
            # Keep None distinct from an empty conclusion for downstream
            # diagnostics while the contract-facing field uses an empty value.
            "metadata": {
                "raw_conclusion": raw_conclusion,
                "github_conclusion": raw_conclusion,
                "raw_status": raw_status,
                "run_url": payload.get("html_url"),
            },
        },
    )


def _map_issue(payload: Mapping[str, Any]) -> GitHubIssueSignal:
    if not isinstance(payload, Mapping):
        raise GitHubAdapterError("GitHub issue must be an object")
    state = _required_string(payload, "state")
    if state not in {"open", "closed"}:
        raise GitHubAdapterError("GitHub issue state is unsupported")
    return _build(
        GitHubIssueSignal,
        {
            "issue_id": _identifier(payload, "number", "id", "issue_id"),
            "title": _required_string(payload, "title"),
            "body": _optional_string(payload, "body"),
            "state": state,
            "observed_at": _timestamp(payload, "updated_at", "created_at"),
            "metadata": {"issue_url": payload.get("html_url")},
        },
    )


def _entries(payload: JSON, key: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, Mapping) and isinstance(payload.get(key), list):
        values = payload[key]
    else:
        raise GitHubAdapterError(f"GitHub response must contain a {key} list")
    if any(not isinstance(item, Mapping) for item in values):
        raise GitHubAdapterError(f"GitHub response {key} entries must be objects")
    return values


def parse_ci_runs(payload: JSON) -> list[CIRunSignal]:
    """Map a GitHub Actions runs response into local CI signals."""
    return [_map_ci_run(item) for item in _entries(payload, "workflow_runs")]


def parse_issues(payload: JSON) -> list[IssueSignal]:
    """Map a GitHub issues response into local issue signals.

    GitHub's ``/issues`` endpoint includes pull requests; entries carrying the
    ``pull_request`` marker are ignored rather than relabelled as issues.
    """
    return [_map_issue(item) for item in _entries(payload, "issues") if "pull_request" not in item]


class GitHubReadOnlyAdapter:
    """Fetch only GitHub runs and issues through an injected transport."""

    def __init__(self, transport: ReadonlyGitHubTransport) -> None:
        self.transport = transport

    @staticmethod
    def _argv(endpoint: str) -> tuple[str, ...]:
        _validate_endpoint(endpoint)
        return (*_ARGV_PREFIX, endpoint, "--input", "-")

    def list_ci_runs(self, owner: str, repository: str) -> list[CIRunSignal]:
        endpoint = _repository_endpoint(owner, repository, "actions/runs")
        return parse_ci_runs(self.transport.run(self._argv(endpoint)))

    def list_issues(self, owner: str, repository: str) -> list[IssueSignal]:
        endpoint = _repository_endpoint(owner, repository, "issues")
        return parse_issues(self.transport.run(self._argv(endpoint)))


__all__ = [
    "FixtureTransport",
    "GitHubAdapterError",
    "GitHubCIRunSignal",
    "GitHubIssueSignal",
    "GitHubReadOnlyAdapter",
    "GhApiTransport",
    "JSON",
    "ReadonlyGitHubTransport",
    "parse_ci_runs",
    "parse_issues",
]
