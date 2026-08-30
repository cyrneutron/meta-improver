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
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field


class HaCliError(RuntimeError):
    """Raised when the pinned provider cannot satisfy its contract."""


class HaCliStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"


class HaCliConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    executable: Path
    cli_entry: Path
    build_id_file: Path
    expected_version: str = Field(min_length=1, max_length=100)
    expected_build_id: str = Field(min_length=1, max_length=200)
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


_ALLOWED: dict[tuple[str, ...], str] = {
    ("capabilities",): "capabilities",
    ("squad", "list"): "squad-list",
    ("squad", "inspect"): "squad-inspect",
    ("squad", "status"): "squad-status",
}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


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
        result = self.transport.run(
            argv, cwd=root, env={}, timeout_seconds=self.config.timeout_seconds,
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
        if parts[:2] == ("squad", "inspect") and len(parts) != 3:
            route = None
        elif parts[:2] == ("squad", "status") and len(parts) != 3:
            route = None
        elif parts in (("capabilities",), ("squad", "list")):
            pass
        elif parts[:2] not in (("squad", "inspect"), ("squad", "status")):
            route = None
        if len(parts) == 3 and not _IDENTIFIER.fullmatch(parts[2]):
            route = None
        if route is None:
            return HaCliReceipt(status=HaCliStatus.UNSUPPORTED, command=list(parts), provider_version=version,
                                provider_build_id=build_id, reason="command arguments are not contracted")
        code, payload, stderr = self._run_json(root, parts)
        receipt_schema = payload.get("schema")
        if parts != ("capabilities",) and receipt_schema != "command-receipt/v2":
            raise HaCliError("HA CLI returned an unsupported receipt schema")
        ok = code == 0 if parts == ("capabilities",) else payload.get("ok") is True
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
