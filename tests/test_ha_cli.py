import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.cli import app
from src.ha_cli import (
    HaCliAdapter,
    HaCliConfig,
    HaCliError,
    HaCliStatus,
    HaCliStatusPollReceipt,
    ProcessResult,
    SubprocessTransport,
)


BUILD = "build-123"


class FixtureTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        payload = self.responses.pop(0)
        return ProcessResult(0, json.dumps(payload).encode(), b"")


def _config(tmp_path: Path) -> HaCliConfig:
    executable = tmp_path / "node"
    entry = tmp_path / "cli.js"
    stamp = tmp_path / "build-id.txt"
    executable.write_text("fixture")
    entry.write_text("fixture")
    stamp.write_text(BUILD)
    return HaCliConfig(executable=executable, cli_entry=entry, build_id_file=stamp,
                       expected_version="0.1.0", expected_build_id=BUILD)


def test_adapter_uses_argv_empty_environment_and_validates_receipt(tmp_path: Path) -> None:
    transport = FixtureTransport([
        {"ok": True, "command": "version", "version": "0.1.0"},
        {"schema": "command-receipt/v2", "ok": True, "command": "squad-list", "items": []},
    ])
    receipt = HaCliAdapter(_config(tmp_path), transport).squad_list(tmp_path)
    assert receipt.status is HaCliStatus.SUCCEEDED
    assert transport.calls[1][0][-3:] == ["squad", "list", "--json"]
    assert transport.calls[1][1]["env"] == {}
    assert transport.calls[1][1]["timeout_seconds"] == 30


def test_uncontracted_command_is_explicitly_unsupported_without_invocation(tmp_path: Path) -> None:
    transport = FixtureTransport([{"ok": True, "command": "version", "version": "0.1.0"}])
    receipt = HaCliAdapter(_config(tmp_path), transport).invoke(tmp_path, ("squad", "run", "x"))
    assert receipt.status is HaCliStatus.UNSUPPORTED
    assert len(transport.calls) == 1


def test_squad_run_uses_fixed_argv_and_repository_relative_cwd(tmp_path: Path) -> None:
    transport = FixtureTransport([
        {"ok": True, "command": "version", "version": "0.1.0"},
        {"schema": "command-receipt/v2", "ok": True, "command": "squad-run", "runId": "run-1"},
    ])
    receipt = HaCliAdapter(_config(tmp_path), transport).squad_run(
        tmp_path, "squad-1", "instance-1", "src/work", "task-1", "prompt-1"
    )
    assert receipt.status is HaCliStatus.SUCCEEDED
    assert transport.calls[1][0][-12:] == [
        "squad", "run", "squad-1", "--instance", "instance-1", "--cwd", "src/work", "--task", "task-1", "--prompt", "prompt-1", "--json"
    ]
    assert transport.calls[1][1]["env"] == {}


@pytest.mark.parametrize("cwd", ["/tmp/outside", "../outside", "src/../outside", "C:\\tmp\\outside"])
def test_squad_run_rejects_non_repository_relative_cwd(tmp_path: Path, cwd: str) -> None:
    transport = FixtureTransport([{"ok": True, "command": "version", "version": "0.1.0"}])
    receipt = HaCliAdapter(_config(tmp_path), transport).squad_run(tmp_path, "squad-1", "instance-1", cwd, "task-1", "prompt-1")
    assert receipt.status is HaCliStatus.UNSUPPORTED
    assert len(transport.calls) == 1


def test_squad_run_rejects_arbitrary_flags(tmp_path: Path) -> None:
    transport = FixtureTransport([{"ok": True, "command": "version", "version": "0.1.0"}])
    receipt = HaCliAdapter(_config(tmp_path), transport).invoke(
        tmp_path,
        ("squad", "run", "squad-1", "--instance", "instance-1", "--cwd", "src", "--task", "task-1", "--prompt", "prompt-1", "--network"),
    )
    assert receipt.status is HaCliStatus.UNSUPPORTED


def test_squad_status_poll_stops_at_terminal_state_and_returns_json_receipts(tmp_path: Path) -> None:
    transport = FixtureTransport([
        {"ok": True, "command": "version", "version": "0.1.0"},
        {"schema": "command-receipt/v2", "ok": True, "status": "running"},
        {"ok": True, "command": "version", "version": "0.1.0"},
        {"schema": "command-receipt/v2", "ok": True, "status": "completed", "runId": "run-1"},
    ])
    receipt = HaCliAdapter(_config(tmp_path), transport).poll_squad_status(
        tmp_path, "run-1", interval_seconds=0, max_attempts=4, deadline_seconds=2
    )
    assert isinstance(receipt, HaCliStatusPollReceipt)
    assert receipt.terminal is True
    assert receipt.attempts == 2
    assert len(receipt.receipts) == 2
    assert receipt.model_dump(by_alias=True)["schema"] == "ha-cli-status-poll-receipt/v1"


def test_squad_status_poll_is_bounded_by_attempts(tmp_path: Path) -> None:
    responses = []
    for _ in range(2):
        responses.extend([
            {"ok": True, "command": "version", "version": "0.1.0"},
            {"schema": "command-receipt/v2", "ok": True, "status": "running"},
        ])
    transport = FixtureTransport(responses)
    receipt = HaCliAdapter(_config(tmp_path), transport).poll_squad_status(
        tmp_path, "run-1", interval_seconds=0, max_attempts=2, deadline_seconds=2
    )
    assert receipt.status is HaCliStatus.REJECTED
    assert receipt.terminal is False
    assert receipt.reason and "bound" in receipt.reason


def test_version_and_build_drift_fail_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.build_id_file.write_text("different")
    with pytest.raises(HaCliError, match="build id drift"):
        HaCliAdapter(config, FixtureTransport([])).capabilities(tmp_path)


def test_cli_help_exposes_only_diagnostic_surface() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "proposal-only" in result.stdout
    assert "ha" in result.stdout
    assert "merge" not in result.stdout.lower()


def test_subprocess_transport_enforces_output_limit(tmp_path: Path) -> None:
    with pytest.raises(HaCliError, match="output limit"):
        SubprocessTransport().run(
            (sys.executable, "-c", "print('x' * 1000)"), cwd=tmp_path, env={},
            timeout_seconds=1, max_output_bytes=100,
        )


def test_subprocess_transport_enforces_timeout(tmp_path: Path) -> None:
    with pytest.raises(HaCliError, match="timed out"):
        SubprocessTransport().run(
            (sys.executable, "-c", "import time; time.sleep(1)"), cwd=tmp_path, env={},
            timeout_seconds=0.02, max_output_bytes=100,
        )
