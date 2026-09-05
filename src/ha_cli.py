"""Bounded, argv-only adapter for the pinned Harness Anything CLI."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field


class HaCliError(RuntimeError):
    """Raised when the pinned provider cannot satisfy its contract."""


class HaCliStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"
    INDETERMINATE = "indeterminate"


class HaCliConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    executable: Path
    cli_entry: Path
    build_id_file: Path
    expected_version: str = Field(min_length=1, max_length=100)
    expected_build_id: str = Field(min_length=1, max_length=200)
    daemon_user_root: Path | None = None
    daemon_id: str = Field(default="default", min_length=1, max_length=100)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_output_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


class HaCliTransport(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProcessResult: ...


class SubprocessTransport:
    """Execute one argv vector while bounding time and combined output."""

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
            list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
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
                    raise HaCliError("HA CLI timed out")
                for key, _ in selector.select(remaining):
                    data = os.read(key.fileobj.fileno(), min(65_536, max_output_bytes + 1))
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(data)
                    if size > max_output_bytes:
                        raise HaCliError("HA CLI output limit exceeded")
                    chunks[key.data].append(data)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HaCliError("HA CLI timed out")
            exit_code = process.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, HaCliError) as exc:
            process.kill()
            process.wait()
            if isinstance(exc, HaCliError):
                raise
            raise HaCliError("HA CLI timed out") from exc
        finally:
            selector.close()
        return ProcessResult(exit_code, b"".join(chunks["stdout"]), b"".join(chunks["stderr"]))


class HaCliReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: str = Field(default="ha-cli-adapter-receipt/v1", alias="schema", serialization_alias="schema")
    status: HaCliStatus
    command: list[str]
    provider_version: str
    provider_build_id: str
    exit_code: int | None = None
    receipt: dict[str, Any] | None = None
    stderr: str = ""
    reason: str | None = None


class HaCliStatusPollReceipt(HaCliReceipt):
    """Bounded read-only status polling receipt.

    ``receipts`` keeps the provider JSON responses (already bounded by the
    transport) so callers can audit each status observation without having to
    invoke the provider themselves.
    """

    schema_: str = Field(
        default="ha-cli-status-poll-receipt/v1", alias="schema", serialization_alias="schema"
    )
    run_id: str
    attempts: int = Field(ge=0)
    terminal: bool
    receipts: list[dict[str, Any]] = Field(default_factory=list, max_length=1_000)


_ALLOWED: dict[tuple[str, ...], str] = {
    ("capabilities",): "capabilities",
    ("squad", "list"): "squad-list",
    ("squad", "inspect"): "squad-inspect",
    ("squad", "status"): "squad-status",
    ("squad", "run"): "squad-run",
}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SAFE_PROMPT = re.compile(r"^[^\x00\r\n]{1,2000}$")
_TERMINAL_STATES = frozenset({
    "completed", "complete", "succeeded", "success", "failed", "failure",
    "rejected", "cancelled", "canceled", "errored", "error", "aborted",
    "done", "terminal", "converged",
})
_DAEMON_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _repository_relative(value: str) -> bool:
    """Return whether *value* is a safe POSIX path relative to repository root."""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return False
    if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", value):
        return False
    pure = PurePosixPath(value)
    return value == "." or (
        not pure.is_absolute()
        and all(part not in {"", ".", ".."} for part in pure.parts)
        and len(value) <= 500
    )


def _safe_prompt(value: str) -> bool:
    return isinstance(value, str) and bool(_SAFE_PROMPT.fullmatch(value)) and not value.startswith("-")


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


class HaCliAdapter:
    def __init__(self, config: HaCliConfig, transport: HaCliTransport | None = None) -> None:
        self.config = config
        self.transport = transport or SubprocessTransport()

    def _provider_build_id(self) -> str:
        for path in (self.config.executable, self.config.cli_entry, self.config.build_id_file):
            if not path.is_absolute() or not path.is_file():
                raise HaCliError(f"pinned provider file is unavailable: {path}")
        build_id = self.config.build_id_file.read_text(encoding="utf-8").strip()
        if build_id != self.config.expected_build_id:
            raise HaCliError("HA CLI build id drift detected")
        return build_id

    def _run_json(self, root: Path, command: Sequence[str]) -> tuple[int, dict[str, Any], str]:
        if not root.is_absolute() or not root.is_dir():
            raise HaCliError("HA root must be an existing absolute directory")
        argv = [str(self.config.executable), str(self.config.cli_entry), "--root", str(root), *command, "--json"]
        env: dict[str, str] = {}
        if self.config.daemon_user_root is not None:
            if not self.config.daemon_user_root.is_absolute() or not self.config.daemon_user_root.is_dir():
                raise HaCliError("configured daemon user root must be an existing absolute directory")
            if _DAEMON_ID.fullmatch(self.config.daemon_id) is None:
                raise HaCliError("configured daemon id is not safe")
            env = {
                "HARNESS_DAEMON_USER_ROOT": str(self.config.daemon_user_root),
                "HARNESS_DAEMON_ID": self.config.daemon_id,
            }
        result = self.transport.run(
            argv, cwd=root, env=env, timeout_seconds=self.config.timeout_seconds,
            max_output_bytes=self.config.max_output_bytes,
        )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HaCliError("HA CLI did not return one JSON document") from exc
        if not isinstance(payload, dict):
            raise HaCliError("HA CLI JSON result must be an object")
        return result.exit_code, payload, result.stderr.decode("utf-8", errors="replace")

    def verify_provider(self, root: Path) -> tuple[str, str]:
        build_id = self._provider_build_id()
        code, payload, _ = self._run_json(root, ["version"])
        if code != 0 or payload.get("ok") is not True or payload.get("version") != self.config.expected_version:
            raise HaCliError("HA CLI version drift detected")
        return self.config.expected_version, build_id

    def invoke(self, root: Path, command: Sequence[str]) -> HaCliReceipt:
        version, build_id = self.verify_provider(root)
        parts = tuple(command)
        route = next((name for prefix, name in _ALLOWED.items() if parts[: len(prefix)] == prefix), None)
        if route is None:
            return HaCliReceipt(status=HaCliStatus.UNSUPPORTED, command=list(parts), provider_version=version,
                                provider_build_id=build_id, reason="command is outside the controlled adapter surface")
        if parts in (("capabilities",), ("squad", "list")):
            pass
        elif parts[:2] in (("squad", "inspect"), ("squad", "status")):
            if len(parts) != 3 or not _identifier(parts[2]):
                route = None
        elif parts[:2] == ("squad", "run"):
            # Keep this shape deliberately literal.  In particular, callers
            # cannot add provider flags or reorder the four required fields.
            if (
                len(parts) != 13
                or not _identifier(parts[2])
                or parts[3] != "--instance"
                or not _identifier(parts[4])
                or parts[5] != "--cwd"
                or not _repository_relative(parts[6])
                or parts[7] != "--permission-mode"
                or parts[8] != "bypass"
                or parts[9] != "--task"
                or not _identifier(parts[10])
                or parts[11] != "--prompt"
                or not _safe_prompt(parts[12])
            ):
                route = None
        else:
            route = None
        if route is None:
            return HaCliReceipt(status=HaCliStatus.UNSUPPORTED, command=list(parts), provider_version=version,
                                provider_build_id=build_id, reason="command arguments are not contracted")
        code, payload, stderr = self._run_json(root, parts)
        receipt_schema = payload.get("schema")
        if parts != ("capabilities",) and receipt_schema != "command-receipt/v2":
            raise HaCliError("HA CLI returned an unsupported receipt schema")
        async_squad_started = (
            parts[:2] == ("squad", "run")
            and payload.get("outcome") == "running"
            and isinstance(payload.get("squadRunId"), str)
            and bool(payload["squadRunId"])
        )
        ok = code == 0 if parts == ("capabilities",) else payload.get("ok") is True or async_squad_started
        return HaCliReceipt(
            status=HaCliStatus.SUCCEEDED if code == 0 and ok else HaCliStatus.REJECTED,
            command=list(parts), provider_version=version, provider_build_id=build_id,
            exit_code=code, receipt=payload, stderr=stderr,
            reason=None if code == 0 and ok else "HA CLI rejected the command",
        )

    def capabilities(self, root: Path) -> HaCliReceipt:
        return self.invoke(root, ("capabilities",))

    def squad_list(self, root: Path) -> HaCliReceipt:
        return self.invoke(root, ("squad", "list"))

    def squad_inspect(self, root: Path, squad_id: str) -> HaCliReceipt:
        return self.invoke(root, ("squad", "inspect", squad_id))

    def squad_status(self, root: Path, run_id: str) -> HaCliReceipt:
        return self.invoke(root, ("squad", "status", run_id))

    def squad_run(
        self,
        root: Path,
        squad_id: str,
        instance: str,
        cwd: str,
        task_id: str,
        task: str,
    ) -> HaCliReceipt:
        """Submit one fixed, proposal-only squad invocation.

        The adapter passes values as argv entries to the pinned HA provider;
        it never interprets ``task`` as a shell command and never enables
        arbitrary provider flags.
        """
        return self.invoke(
            root,
            (
                "squad", "run", squad_id, "--instance", instance, "--cwd", cwd,
                "--permission-mode", "bypass", "--task", task_id, "--prompt", task,
            ),
        )

    def poll_squad_status(
        self,
        root: Path,
        run_id: str,
        *,
        interval_seconds: float = 1.0,
        max_attempts: int = 30,
        deadline_seconds: float = 30.0,
    ) -> HaCliStatusPollReceipt:
        """Poll only ``squad status`` until a terminal state or a bound fires."""
        if not _identifier(run_id):
            raise HaCliError("run_id is not a contracted identifier")
        if not isinstance(interval_seconds, (int, float)) or isinstance(interval_seconds, bool) or not 0 <= interval_seconds <= 60:
            raise HaCliError("interval_seconds must be between 0 and 60")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 1_000:
            raise HaCliError("max_attempts must be between 1 and 1000")
        if not isinstance(deadline_seconds, (int, float)) or isinstance(deadline_seconds, bool) or not 0 < deadline_seconds <= 300:
            raise HaCliError("deadline_seconds must be between 0 and 300")

        started = time.monotonic()
        observations: list[HaCliReceipt] = []
        latest: HaCliReceipt | None = None
        for attempt in range(1, max_attempts + 1):
            if time.monotonic() - started >= deadline_seconds:
                break
            latest = self.squad_status(root, run_id)
            observations.append(latest)
            payload = latest.receipt or {}
            raw_state = payload.get("status", payload.get("state", payload.get("phase")))
            state = raw_state.casefold() if isinstance(raw_state, str) else ""
            terminal = payload.get("terminal") is True or state in _TERMINAL_STATES
            if terminal:
                return HaCliStatusPollReceipt(
                    status=latest.status,
                    command=latest.command,
                    provider_version=latest.provider_version,
                    provider_build_id=latest.provider_build_id,
                    exit_code=latest.exit_code,
                    receipt=payload,
                    stderr=latest.stderr,
                    reason=latest.reason,
                    run_id=run_id,
                    attempts=attempt,
                    terminal=True,
                    receipts=[item.receipt or {} for item in observations],
                )
            if latest.status is not HaCliStatus.SUCCEEDED:
                return HaCliStatusPollReceipt(
                    status=latest.status,
                    command=latest.command,
                    provider_version=latest.provider_version,
                    provider_build_id=latest.provider_build_id,
                    exit_code=latest.exit_code,
                    receipt=payload,
                    stderr=latest.stderr,
                    reason=latest.reason,
                    run_id=run_id,
                    attempts=attempt,
                    terminal=False,
                    receipts=[item.receipt or {} for item in observations],
                )
            remaining = deadline_seconds - (time.monotonic() - started)
            if attempt < max_attempts and remaining > 0 and interval_seconds:
                time.sleep(min(interval_seconds, remaining))

        if latest is None:
            raise HaCliError("squad status polling deadline exceeded before first observation")
        return HaCliStatusPollReceipt(
            status=HaCliStatus.INDETERMINATE,
            command=latest.command,
            provider_version=latest.provider_version,
            provider_build_id=latest.provider_build_id,
            exit_code=latest.exit_code,
            receipt=latest.receipt,
            stderr=latest.stderr,
            reason="squad status polling deadline or attempt bound exceeded before a terminal state was observed",
            run_id=run_id,
            attempts=len(observations),
            terminal=False,
            receipts=[item.receipt or {} for item in observations],
        )

    # Short aliases make the polling operation discoverable without exposing a
    # second implementation or a mutable provider client.
    squad_status_poll = poll_squad_status
