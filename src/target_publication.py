"""Controlled target pull-request publication and required-check evidence.

This module owns the only GitHub write boundary in MI.  It does not merge,
approve, or alter protected branches.  A caller must supply an already
accepted target commit; publication is limited to a deterministic ``mi/``
branch and a matching pull request.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.ha_cli import ProcessResult


_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_BRANCH_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECK_NAME = re.compile(r"^[^\x00\r\n]{1,200}$")
_CHECK_STATE = re.compile(r"^[A-Z_]{1,40}$")
_HTTP_NOT_FOUND = re.compile(r"(?:\bHTTP\s+404\b|\b404\s+Not Found\b)", re.IGNORECASE)
_SECRET = re.compile(
    r"(?:bearer\s+\S+|(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,})",
    re.IGNORECASE,
)


class TargetPublicationError(RuntimeError):
    """Raised when MI cannot prove a bounded publication action is safe."""


class TargetPublicationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    PENDING = "pending"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_text(value: str, field_name: str, *, limit: int, multiline: bool) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{field_name} must be bounded non-empty text")
    if not multiline and ("\r" in value or "\n" in value):
        raise ValueError(f"{field_name} must be one line")
    if _SECRET.search(value):
        raise ValueError(f"{field_name} contains secret-shaped text")
    return value


def _safe_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return value.astimezone(timezone.utc)


class TargetPublicationRequest(BaseModel):
    """The hash-bound publication input produced after target acceptance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    target_task_id: str
    accepted_execution_id: str
    accepted_commit: str
    title: str
    body: str
    base_branch: str = "main"
    max_poll_attempts: int = Field(default=30, ge=1, le=120)
    poll_interval_seconds: float = Field(default=5.0, ge=0, le=60)

    @field_validator("repository")
    @classmethod
    def repository_is_safe(cls, value: str) -> str:
        if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
            raise ValueError("repository must be an owner/name identifier")
        return value

    @field_validator("target_task_id", "accepted_execution_id")
    @classmethod
    def identifier_is_safe(cls, value: str, info: Any) -> str:
        if not isinstance(value, str) or _TASK_ID.fullmatch(value) is None:
            raise ValueError(f"{info.field_name} must be a bounded identifier")
        return value

    @field_validator("target_task_id")
    @classmethod
    def target_task_is_git_branch_safe(cls, value: str) -> str:
        if _BRANCH_TASK_ID.fullmatch(value) is None:
            raise ValueError("target_task_id must be safe to derive an MI Git branch")
        return value

    @field_validator("accepted_commit")
    @classmethod
    def commit_is_full_sha(cls, value: str) -> str:
        if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
            raise ValueError("accepted_commit must be a lowercase full 40-character SHA")
        return value

    @field_validator("title")
    @classmethod
    def title_is_safe(cls, value: str) -> str:
        return _safe_text(value, "title", limit=200, multiline=False)

    @field_validator("body")
    @classmethod
    def body_is_safe(cls, value: str) -> str:
        return _safe_text(value, "body", limit=20_000, multiline=True)

    @field_validator("base_branch")
    @classmethod
    def base_branch_is_main(cls, value: str) -> str:
        if value != "main":
            raise ValueError("base_branch is currently fixed to protected main")
        return value

    @property
    def head_branch(self) -> str:
        return f"mi/{self.target_task_id}"

    @property
    def expected_origin(self) -> str:
        return f"https://github.com/{self.repository}.git"

    @property
    def request_hash(self) -> str:
        return _digest(self.model_dump(mode="json"))


class RequiredCheck(BaseModel):
    """One required-check observation from ``gh pr checks --required``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    state: str
    link: str | None = None

    @field_validator("name")
    @classmethod
    def name_is_safe(cls, value: str) -> str:
        if not isinstance(value, str) or _CHECK_NAME.fullmatch(value) is None or _SECRET.search(value):
            raise ValueError("check name is unsafe")
        return value

    @field_validator("state")
    @classmethod
    def state_is_safe(cls, value: str) -> str:
        if not isinstance(value, str) or _CHECK_STATE.fullmatch(value) is None:
            raise ValueError("check state is invalid")
        return value

    @field_validator("link")
    @classmethod
    def link_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 500 or not value.startswith("https://github.com/"):
            raise ValueError("check link must be a bounded GitHub HTTPS URL")
        return value


class TargetPublicationReceipt(BaseModel):
    """A redacted, hash-bound record of one publication outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: str = Field(
        default="target-publication-receipt/v1", alias="schema", serialization_alias="schema"
    )
    status: TargetPublicationStatus
    request_hash: str = Field(pattern=_DIGEST.pattern)
    repository: str
    target_task_id: str
    accepted_execution_id: str
    accepted_commit: str = Field(pattern=_COMMIT.pattern)
    head_branch: str
    base_branch: str = "main"
    pr_number: int | None = Field(default=None, ge=1)
    pr_url: str | None = None
    checks: list[RequiredCheck] = Field(default_factory=list, max_length=200)
    poll_attempts: int = Field(default=0, ge=0, le=120)
    reason: str = Field(default="", max_length=2_000)
    observed_at: datetime
    receipt_hash: str | None = Field(default=None, pattern=_DIGEST.pattern)

    @field_validator("repository")
    @classmethod
    def receipt_repository_is_safe(cls, value: str) -> str:
        return TargetPublicationRequest.repository_is_safe(value)

    @field_validator("target_task_id", "accepted_execution_id")
    @classmethod
    def receipt_identifier_is_safe(cls, value: str, info: Any) -> str:
        return TargetPublicationRequest.identifier_is_safe(value, info)

    @field_validator("head_branch")
    @classmethod
    def head_branch_is_safe(cls, value: str) -> str:
        if not isinstance(value, str) or not value.startswith("mi/") or _TASK_ID.fullmatch(value[3:]) is None:
            raise ValueError("head_branch must be an MI task branch")
        return value

    @field_validator("base_branch")
    @classmethod
    def receipt_base_branch_is_main(cls, value: str) -> str:
        if value != "main":
            raise ValueError("base_branch must be main")
        return value

    @field_validator("pr_url")
    @classmethod
    def pr_url_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 500 or not value.startswith("https://github.com/"):
            raise ValueError("pr_url must be a bounded GitHub HTTPS URL")
        return value

    @field_validator("reason")
    @classmethod
    def reason_is_safe(cls, value: str) -> str:
        if value and _SECRET.search(value):
            raise ValueError("reason contains secret-shaped text")
        return value

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_utc(cls, value: datetime) -> datetime:
        return _safe_utc(value)

    @model_validator(mode="after")
    def validate_bindings_and_hash(self) -> TargetPublicationReceipt:
        if _BRANCH_TASK_ID.fullmatch(self.target_task_id) is None:
            raise ValueError("receipt target task is unsafe to derive an MI Git branch")
        if self.head_branch != f"mi/{self.target_task_id}":
            raise ValueError("receipt head branch is not bound to target task")
        if self.pr_number is None and self.pr_url is not None:
            raise ValueError("pull request URL requires a pull request number")
        if self.pr_number is not None:
            expected_url = f"https://github.com/{self.repository}/pull/{self.pr_number}"
            if self.pr_url != expected_url:
                raise ValueError("pull request URL is not bound to repository and number")
        if len({check.name.casefold() for check in self.checks}) != len(self.checks):
            raise ValueError("required checks must not contain duplicate names")
        if self.status is TargetPublicationStatus.SUCCEEDED:
            if self.pr_number is None or self.pr_url is None or not self.checks:
                raise ValueError("successful publication requires a pull request and required checks")
            if any(check.state != "SUCCESS" for check in self.checks):
                raise ValueError("successful publication requires all required checks to succeed")
            if self.reason:
                raise ValueError("successful publication cannot have a rejection reason")
        elif not self.reason:
            raise ValueError("non-successful publication requires a reason")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"}))
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("receipt_hash does not match canonical contents")
        object.__setattr__(self, "receipt_hash", expected)
        return self


class TargetPublicationLedger(Protocol):
    def record_target_publication(self, receipt: TargetPublicationReceipt) -> TargetPublicationReceipt: ...


class PublicationTransport(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProcessResult: ...


class SubprocessPublicationTransport:
    """Run one fixed argv vector with bounded combined output and no shell."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProcessResult:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        size = 0
        deadline = time.monotonic() + timeout_seconds
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TargetPublicationError("publication command timed out")
                for key, _ in selector.select(remaining):
                    data = os.read(key.fileobj.fileno(), min(65_536, max_output_bytes + 1))
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(data)
                    if size > max_output_bytes:
                        raise TargetPublicationError("publication command output limit exceeded")
                    chunks[key.data].append(data)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TargetPublicationError("publication command timed out")
            exit_code = process.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, TargetPublicationError) as exc:
            process.kill()
            process.wait()
            if isinstance(exc, TargetPublicationError):
                raise
            raise TargetPublicationError("publication command timed out") from exc
        finally:
            selector.close()
        return ProcessResult(exit_code, b"".join(chunks["stdout"]), b"".join(chunks["stderr"]))


class TargetPublicationConfig(BaseModel):
    """Pinned executable identities and transport limits for GitHub publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    git_executable: Path
    git_sha256: str = Field(pattern=_DIGEST.pattern)
    gh_executable: Path
    gh_sha256: str = Field(pattern=_DIGEST.pattern)
    github_home: Path
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_output_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)

    @field_validator("github_home")
    @classmethod
    def github_home_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("github_home must be an absolute directory")
        return value


@dataclass(frozen=True)
class _PullRequest:
    number: int
    url: str


def _redacted_error(value: str) -> str:
    text = _SECRET.sub("[redacted]", value).replace("\r", " ").replace("\n", " ").strip()
    return text[:500] or "external command failed"


class TargetPublisher:
    """Create or reuse one target PR, then collect its required-check receipt."""

    def __init__(
        self, config: TargetPublicationConfig, transport: PublicationTransport | None = None
    ) -> None:
        self.config = config
        self.transport = transport or SubprocessPublicationTransport()

    def publish(
        self,
        target_root: Path,
        request: TargetPublicationRequest,
        *,
        ledger: TargetPublicationLedger | None = None,
    ) -> TargetPublicationReceipt:
        started = datetime.now(timezone.utc)
        try:
            self._verify_executables()
            self._prove_target(target_root, request)
            self._ensure_head_ref(target_root, request)
            pull_request = self._find_or_create_pull_request(target_root, request)
            receipt = self._poll_required_checks(target_root, request, pull_request, started)
        except TargetPublicationError as exc:
            receipt = self._receipt(
                TargetPublicationStatus.REJECTED,
                request,
                started,
                reason=_redacted_error(str(exc)),
            )
        if ledger is not None:
            return ledger.record_target_publication(rehydrate_target_publication(receipt))
        return receipt

    def _verify_executables(self) -> None:
        for executable, expected_digest, label in (
            (self.config.git_executable, self.config.git_sha256, "git"),
            (self.config.gh_executable, self.config.gh_sha256, "gh"),
        ):
            if not executable.is_absolute() or not executable.is_file():
                raise TargetPublicationError(f"pinned {label} executable is unavailable")
            actual_digest = "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
            if actual_digest != expected_digest:
                raise TargetPublicationError(f"pinned {label} executable digest drift detected")
        if not self.config.github_home.is_dir():
            raise TargetPublicationError("configured GitHub home directory is unavailable")

    def _prove_target(self, target_root: Path, request: TargetPublicationRequest) -> None:
        if not target_root.is_absolute() or not target_root.is_dir():
            raise TargetPublicationError("target root must be an existing absolute directory")
        origin = self._git(target_root, "remote", "get-url", "origin").stdout.decode("utf-8", errors="replace").strip()
        if origin != request.expected_origin:
            raise TargetPublicationError("target origin does not match the requested GitHub repository")
        status = self._git(target_root, "status", "--porcelain=v1", "--untracked-files=no")
        if status.stdout.strip():
            raise TargetPublicationError("target worktree has tracked changes")
        self._git(target_root, "cat-file", "-e", f"{request.accepted_commit}^{{commit}}")

    def _ensure_head_ref(self, target_root: Path, request: TargetPublicationRequest) -> None:
        existing = self._optional_gh_json(
            target_root, "api", f"repos/{request.repository}/git/ref/heads/{request.head_branch}"
        )
        if existing is not None:
            sha = ((existing.get("object") or {}).get("sha")) if isinstance(existing, dict) else None
            if sha != request.accepted_commit:
                raise TargetPublicationError("deterministic publication branch exists at a different commit")
            return
        payload = self._gh_json(
            target_root,
            "api",
            "--method",
            "POST",
            f"repos/{request.repository}/git/refs",
            "-f",
            f"ref=refs/heads/{request.head_branch}",
            "-f",
            f"sha={request.accepted_commit}",
        )
        sha = ((payload.get("object") or {}).get("sha")) if isinstance(payload, dict) else None
        if sha != request.accepted_commit:
            raise TargetPublicationError("GitHub did not create the requested branch at the accepted commit")

    def _find_or_create_pull_request(
        self, target_root: Path, request: TargetPublicationRequest
    ) -> _PullRequest:
        matches = self._matching_pull_requests(target_root, request)
        if len(matches) > 1:
            raise TargetPublicationError("multiple open pull requests match the deterministic branch")
        if matches:
            return matches[0]
        result = self._run(
            target_root,
            [
                str(self.config.gh_executable),
                "pr",
                "create",
                "--repo",
                request.repository,
                "--head",
                request.head_branch,
                "--base",
                request.base_branch,
                "--title",
                request.title,
                "--body",
                request.body,
            ],
            env={"HOME": str(self.config.github_home)},
        )
        if result.exit_code != 0:
            # A concurrent identical publisher may have created the PR after
            # the list query. Re-query before deciding that publication failed.
            matches = self._matching_pull_requests(target_root, request)
            if len(matches) == 1:
                return matches[0]
            raise TargetPublicationError("GitHub pull request creation was rejected")
        match = re.search(
            rf"^https://github\.com/{re.escape(request.repository)}/pull/(\d+)\s*$",
            result.stdout.decode("utf-8", errors="replace"),
            re.MULTILINE,
        )
        if match is None:
            raise TargetPublicationError("GitHub pull request creation returned no matching URL")
        pull_request = _PullRequest(int(match.group(1)), match.group(0).strip())
        self._validate_pull_request(target_root, request, pull_request)
        return pull_request

    def _matching_pull_requests(
        self, target_root: Path, request: TargetPublicationRequest
    ) -> list[_PullRequest]:
        payload = self._gh_json(
            target_root,
            "pr",
            "list",
            "--repo",
            request.repository,
            "--head",
            request.head_branch,
            "--base",
            request.base_branch,
            "--state",
            "open",
            "--json",
            "number,url,headRefName,baseRefName,headRefOid",
        )
        if not isinstance(payload, list):
            raise TargetPublicationError("GitHub pull request list did not return an array")
        matches: list[_PullRequest] = []
        for item in payload:
            if not isinstance(item, dict):
                raise TargetPublicationError("GitHub pull request list contains an invalid item")
            if (
                item.get("headRefName") != request.head_branch
                or item.get("baseRefName") != request.base_branch
                or item.get("headRefOid") != request.accepted_commit
            ):
                continue
            number, url = item.get("number"), item.get("url")
            if not isinstance(number, int) or not isinstance(url, str):
                raise TargetPublicationError("matching pull request is missing identity fields")
            if url != f"https://github.com/{request.repository}/pull/{number}":
                raise TargetPublicationError("matching pull request URL is not bound to repository and number")
            matches.append(_PullRequest(number, url))
        return matches

    def _validate_pull_request(
        self, target_root: Path, request: TargetPublicationRequest, pull_request: _PullRequest
    ) -> None:
        payload = self._gh_json(
            target_root,
            "pr",
            "view",
            str(pull_request.number),
            "--repo",
            request.repository,
            "--json",
            "number,url,headRefName,baseRefName,headRefOid,state",
        )
        if not isinstance(payload, dict):
            raise TargetPublicationError("GitHub pull request view did not return an object")
        if (
            payload.get("number") != pull_request.number
            or payload.get("url") != pull_request.url
            or payload.get("state") != "OPEN"
            or payload.get("headRefName") != request.head_branch
            or payload.get("baseRefName") != request.base_branch
            or payload.get("headRefOid") != request.accepted_commit
        ):
            raise TargetPublicationError("GitHub pull request identity is not bound to the accepted commit")

    def _poll_required_checks(
        self,
        target_root: Path,
        request: TargetPublicationRequest,
        pull_request: _PullRequest,
        observed_at: datetime,
    ) -> TargetPublicationReceipt:
        last_checks: list[RequiredCheck] = []
        for attempt in range(1, request.max_poll_attempts + 1):
            payload = self._gh_json(
                target_root,
                "pr",
                "checks",
                str(pull_request.number),
                "--repo",
                request.repository,
                "--required",
                "--json",
                "name,state,link",
                allow_failure=True,
            )
            if not isinstance(payload, list):
                return self._receipt(
                    TargetPublicationStatus.REJECTED,
                    request,
                    observed_at,
                    pull_request=pull_request,
                    attempts=attempt,
                    reason="GitHub required-check query did not return an array",
                )
            try:
                last_checks = [RequiredCheck.model_validate(item) for item in payload]
            except Exception as exc:
                return self._receipt(
                    TargetPublicationStatus.REJECTED,
                    request,
                    observed_at,
                    pull_request=pull_request,
                    attempts=attempt,
                    reason=_redacted_error(f"GitHub required-check payload is invalid: {exc}"),
                )
            if not last_checks or len({check.name.casefold() for check in last_checks}) != len(last_checks):
                return self._receipt(
                    TargetPublicationStatus.REJECTED,
                    request,
                    observed_at,
                    pull_request=pull_request,
                    attempts=attempt,
                    checks=last_checks,
                    reason="GitHub did not provide one unambiguous required-check set",
                )
            states = {check.state for check in last_checks}
            if states == {"SUCCESS"}:
                return self._receipt(
                    TargetPublicationStatus.SUCCEEDED,
                    request,
                    observed_at,
                    pull_request=pull_request,
                    attempts=attempt,
                    checks=last_checks,
                )
            if states & {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR", "SKIPPED"}:
                return self._receipt(
                    TargetPublicationStatus.REJECTED,
                    request,
                    observed_at,
                    pull_request=pull_request,
                    attempts=attempt,
                    checks=last_checks,
                    reason="one or more required GitHub checks did not succeed",
                )
            if not states <= {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}:
                return self._receipt(
                    TargetPublicationStatus.REJECTED,
                    request,
                    observed_at,
                    pull_request=pull_request,
                    attempts=attempt,
                    checks=last_checks,
                    reason="GitHub returned an unknown required-check state",
                )
            if attempt < request.max_poll_attempts and request.poll_interval_seconds:
                time.sleep(request.poll_interval_seconds)
        return self._receipt(
            TargetPublicationStatus.PENDING,
            request,
            observed_at,
            pull_request=pull_request,
            attempts=request.max_poll_attempts,
            checks=last_checks,
            reason="required GitHub checks did not reach a terminal state before the polling bound",
        )

    def _git(self, target_root: Path, *arguments: str) -> ProcessResult:
        result = self._run(
            target_root,
            [str(self.config.git_executable), "-C", str(target_root), *arguments],
            env={},
        )
        if result.exit_code != 0:
            raise TargetPublicationError("target Git identity check was rejected")
        return result

    def _optional_gh_json(self, target_root: Path, *arguments: str) -> Any | None:
        result = self._run(
            target_root,
            [str(self.config.gh_executable), *arguments],
            env={"HOME": str(self.config.github_home)},
        )
        if result.exit_code != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            if _HTTP_NOT_FOUND.search(stderr):
                return None
            raise TargetPublicationError("GitHub branch existence request was rejected")
        return self._decode_json(result, "GitHub API")

    def _gh_json(self, target_root: Path, *arguments: str, allow_failure: bool = False) -> Any:
        result = self._run(
            target_root,
            [str(self.config.gh_executable), *arguments],
            env={"HOME": str(self.config.github_home)},
        )
        payload = self._decode_json(result, "GitHub CLI")
        if result.exit_code != 0 and not allow_failure:
            raise TargetPublicationError("GitHub CLI request was rejected")
        return payload

    def _run(self, cwd: Path, argv: list[str], *, env: Mapping[str, str]) -> ProcessResult:
        return self.transport.run(
            argv,
            cwd=cwd,
            env=env,
            timeout_seconds=self.config.timeout_seconds,
            max_output_bytes=self.config.max_output_bytes,
        )

    @staticmethod
    def _decode_json(result: ProcessResult, label: str) -> Any:
        try:
            return json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TargetPublicationError(f"{label} did not return one JSON document") from exc

    @staticmethod
    def _receipt(
        status: TargetPublicationStatus,
        request: TargetPublicationRequest,
        observed_at: datetime,
        *,
        pull_request: _PullRequest | None = None,
        attempts: int = 0,
        checks: list[RequiredCheck] | None = None,
        reason: str = "",
    ) -> TargetPublicationReceipt:
        return TargetPublicationReceipt(
            status=status,
            request_hash=request.request_hash,
            repository=request.repository,
            target_task_id=request.target_task_id,
            accepted_execution_id=request.accepted_execution_id,
            accepted_commit=request.accepted_commit,
            head_branch=request.head_branch,
            base_branch=request.base_branch,
            pr_number=pull_request.number if pull_request else None,
            pr_url=pull_request.url if pull_request else None,
            checks=checks or [],
            poll_attempts=attempts,
            reason=reason,
            observed_at=observed_at,
        )


def rehydrate_target_publication(value: TargetPublicationReceipt) -> TargetPublicationReceipt:
    """Revalidate a receipt before it crosses into durable ledger storage."""

    if not isinstance(value, TargetPublicationReceipt) or value.receipt_hash is None:
        raise TargetPublicationError("target publication receipt is not hashed")
    canonical = _canonical(value.model_dump(mode="json", by_alias=True))
    try:
        hydrated = TargetPublicationReceipt.model_validate(json.loads(canonical))
    except Exception as exc:
        raise TargetPublicationError("target publication receipt failed integrity rehydration") from exc
    if hydrated.receipt_hash != value.receipt_hash or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise TargetPublicationError("target publication receipt hash does not match canonical contents")
    return hydrated


__all__ = [
    "PublicationTransport",
    "RequiredCheck",
    "SubprocessPublicationTransport",
    "TargetPublicationConfig",
    "TargetPublicationError",
    "TargetPublicationLedger",
    "TargetPublicationReceipt",
    "TargetPublicationRequest",
    "TargetPublicationStatus",
    "TargetPublisher",
    "rehydrate_target_publication",
]
