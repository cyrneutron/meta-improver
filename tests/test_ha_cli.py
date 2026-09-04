import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import src.cli as cli_module
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
from src.models import AttemptStage, AttemptStatus
from src.storage import Ledger


BUILD = "build-123"
CURRENT_VERSION = "0.0.1"
CURRENT_BUILD_ID = "354028c2-1149-449d-abdc-0b07f81c386a"


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


def test_adapter_only_injects_explicit_bounded_daemon_routing(tmp_path: Path) -> None:
    transport = FixtureTransport([
        {"ok": True, "command": "version", "version": "0.1.0"},
        {"schema": "command-receipt/v2", "ok": True, "command": "squad-list", "items": []},
    ])
    config = _config(tmp_path).model_copy(update={"daemon_user_root": tmp_path, "daemon_id": "candidate-1"})
    HaCliAdapter(config, transport).squad_list(tmp_path)
    assert transport.calls[1][1]["env"] == {
        "HARNESS_DAEMON_USER_ROOT": str(tmp_path),
        "HARNESS_DAEMON_ID": "candidate-1",
    }


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


def test_squad_run_accepts_async_running_receipt_with_run_id(tmp_path: Path) -> None:
    transport = FixtureTransport([
        {"ok": True, "command": "version", "version": "0.1.0"},
        {
            "schema": "command-receipt/v2",
            "ok": False,
            "outcome": "running",
            "squadRunId": "run-1",
        },
    ])
    receipt = HaCliAdapter(_config(tmp_path), transport).squad_run(
        tmp_path, "squad-1", "instance-1", ".", "task-1", "prompt-1"
    )
    assert receipt.status is HaCliStatus.SUCCEEDED
    assert receipt.receipt and receipt.receipt["squadRunId"] == "run-1"


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


def test_squad_status_poll_treats_converged_as_terminal(tmp_path: Path) -> None:
    transport = FixtureTransport([
        {"ok": True, "command": "version", "version": "0.1.0"},
        {"schema": "command-receipt/v2", "ok": True, "status": "converged", "decision": {"kind": "converged"}},
    ])
    receipt = HaCliAdapter(_config(tmp_path), transport).poll_squad_status(
        tmp_path, "run-1", interval_seconds=0, max_attempts=1, deadline_seconds=2
    )
    assert receipt.terminal is True


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

    config = _config(tmp_path)
    transport = FixtureTransport([{"ok": True, "command": "version", "version": "different"}])
    with pytest.raises(HaCliError, match="version drift"):
        HaCliAdapter(config, transport).capabilities(tmp_path)


class _CliReceipt:
    def model_dump_json(self, **_kwargs) -> str:
        return json.dumps({"ok": True})


class _CliAdapter:
    def capabilities(self, _root):
        return _CliReceipt()

    def squad_run(self, *_args):
        return _CliReceipt()

    def poll_squad_status(self, *_args, **_kwargs):
        return _CliReceipt()


@pytest.mark.parametrize(
    "command,args",
    [
        ("check", []),
        (
            "squad-run",
            [
                "--squad-id", "squad-1",
                "--instance", "instance-1",
                "--cwd", "src",
                "--task", "task-1",
                "--prompt", "diagnose",
            ],
        ),
        ("squad-status", ["--run-id", "run-1"]),
    ],
)
def test_cli_ha_commands_share_current_default_identity(tmp_path: Path, monkeypatch, command, args) -> None:
    config = _config(tmp_path)
    captured = []

    def capture_adapter(executable, cli_entry, build_id_file, version, build_id):
        captured.append((executable, cli_entry, build_id_file, version, build_id))
        return _CliAdapter()

    monkeypatch.setattr(cli_module, "_adapter", capture_adapter)
    result = CliRunner().invoke(
        app,
        [
            "ha",
            command,
            "--root", str(tmp_path),
            "--executable", str(config.executable),
            "--cli-entry", str(config.cli_entry),
            "--build-id-file", str(config.build_id_file),
            *args,
        ],
    )

    assert result.exit_code == 0
    assert len(captured) == 1
    assert captured[0][3:] == (CURRENT_VERSION, CURRENT_BUILD_ID)


def test_cli_rejects_explicit_wrong_build_identity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "ha",
            "check",
            "--root", str(tmp_path),
            "--executable", str(config.executable),
            "--cli-entry", str(config.cli_entry),
            "--build-id-file", str(config.build_id_file),
            "--version", CURRENT_VERSION,
            "--build-id", "wrong-build-id",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "build id drift" in payload["error"]


def test_cli_squad_diagnose_records_diagnosis_attempt_without_real_provider(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    transport = FixtureTransport(
        [
            {"ok": True, "command": "version", "version": "0.1.0"},
            {"schema": "command-receipt/v2", "ok": True, "squadRunId": "run-1"},
            {"ok": True, "command": "version", "version": "0.1.0"},
            {
                "schema": "command-receipt/v2",
                "ok": True,
                "status": "completed",
                "decision": {"kind": "converged", "summary": "Found the boundary."},
            },
        ]
    )
    fixture_adapter = HaCliAdapter(config, transport)
    identities = []

    def capture_adapter(_executable, _entry, _stamp, version, build_id):
        identities.append((version, build_id))
        return fixture_adapter

    monkeypatch.setattr(cli_module, "_adapter", capture_adapter)
    ledger_path = tmp_path / "history.db"
    result = CliRunner().invoke(
        app,
        [
            "ha",
            "squad-diagnose",
            "--root", str(tmp_path),
            "--executable", str(config.executable),
            "--cli-entry", str(config.cli_entry),
            "--build-id-file", str(config.build_id_file),
            "--ledger", str(ledger_path),
            "--diagnosis-id", "diag-1",
            "--squad-id", "mi-ha-governance",
            "--instance", "leader",
            "--cwd", ".",
            "--task", "task-1",
            "--prompt", "Inspect the repository.",
            "--base-commit", "a" * 40,
            "--strategy-version", "ha-diagnosis-v1",
            "--model-version", "ha-squad/provider-0.1.0",
            "--prompt-version", "diagnosis-prompt-v1",
            "--interval-seconds", "0",
            "--deadline-seconds", "2",
        ],
    )

    assert result.exit_code == 0
    assert identities == [(CURRENT_VERSION, CURRENT_BUILD_ID)]
    payload = json.loads(result.stdout)
    assert payload["schema"] == "ha-diagnosis-attempt-receipt/v1"
    assert payload["diagnosis"]["diagnosis_id"] == "diag-1"
    assert payload["attempt"]["status"] == AttemptStatus.PROPOSED.value
    assert payload["attempt"]["stage"] == AttemptStage.CAPTURED.value
    attempt = Ledger(ledger_path).get_attempt(payload["attempt"]["attempt_id"])
    assert attempt is not None
    assert attempt.source_diagnosis_id == "diag-1"


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
