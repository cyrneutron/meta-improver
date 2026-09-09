"""Small command-line entry point for controlled provider diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from src.ha_cli import HaCliAdapter, HaCliConfig, HaCliError, HaCliPermissionMode
from src.ha_diagnosis import (
    HaDiagnosisError,
    collect_squad_diagnosis,
    record_squad_diagnosis_attempt,
)
from src.acceptance import CandidateAcceptanceAdmission
from src.storage import Ledger, LedgerConflictError
from src.target_publication import (
    TargetPublicationConfig,
    TargetPublicationError,
    TargetPublicationRequest,
    TargetPublicationStatus,
    TargetPublisher,
)


app = typer.Typer(help="Meta-Improver proposal-only control surface.", no_args_is_help=True)
ha_app = typer.Typer(help="Inspect the pinned Harness Anything provider.", no_args_is_help=True)
target_app = typer.Typer(help="Publish accepted target commits through a controlled pull request.", no_args_is_help=True)
app.add_typer(ha_app, name="ha")
app.add_typer(target_app, name="target")

DEFAULT_HA_CLI_VERSION = "0.0.1"
DEFAULT_HA_CLI_BUILD_ID = "1942211c-e727-4c18-867a-268bba08ba12"


def _adapter(
    executable: Path,
    cli_entry: Path,
    build_id_file: Path,
    version: str,
    build_id: str,
    daemon_user_root: Path | None = None,
    daemon_id: str = "default",
    timeout_seconds: float = 30.0,
) -> HaCliAdapter:
    return HaCliAdapter(HaCliConfig(
        executable=executable, cli_entry=cli_entry, build_id_file=build_id_file,
        expected_version=version, expected_build_id=build_id,
        daemon_user_root=daemon_user_root, daemon_id=daemon_id,
        timeout_seconds=timeout_seconds,
    ))


@ha_app.command("check")
def ha_check(
    root: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    executable: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    cli_entry: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    build_id_file: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    daemon_user_root: Annotated[
        Path | None, typer.Option(exists=True, file_okay=False, resolve_path=True)
    ] = None,
    daemon_id: Annotated[str, typer.Option()] = "default",
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=0.001, max=300)] = 30.0,
    version: Annotated[str, typer.Option()] = DEFAULT_HA_CLI_VERSION,
    build_id: Annotated[str, typer.Option()] = DEFAULT_HA_CLI_BUILD_ID,
) -> None:
    """Verify version/build identity and print capabilities as JSON."""
    try:
        receipt = _adapter(
            executable, cli_entry, build_id_file, version, build_id, daemon_user_root, daemon_id,
            timeout_seconds,
        ).capabilities(root)
    except HaCliError as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        raise typer.Exit(1) from exc
    typer.echo(receipt.model_dump_json(by_alias=True))


@ha_app.command("squad-run")
def ha_squad_run(
    root: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    executable: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    cli_entry: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    build_id_file: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    squad_id: Annotated[str, typer.Option("--squad-id")],
    instance: Annotated[str, typer.Option("--instance")],
    cwd: Annotated[str, typer.Option("--cwd")],
    task_id: Annotated[str, typer.Option("--task")],
    task: Annotated[str, typer.Option("--prompt")],
    daemon_user_root: Annotated[
        Path | None, typer.Option(exists=True, file_okay=False, resolve_path=True)
    ] = None,
    daemon_id: Annotated[str, typer.Option()] = "default",
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=0.001, max=300)] = 30.0,
    permission_mode: Annotated[HaCliPermissionMode, typer.Option("--permission-mode")] = "read-only",
    version: Annotated[str, typer.Option()] = DEFAULT_HA_CLI_VERSION,
    build_id: Annotated[str, typer.Option()] = DEFAULT_HA_CLI_BUILD_ID,
) -> None:
    """Emit the receipt for one fixed, proposal-only squad request."""
    try:
        receipt = _adapter(
            executable, cli_entry, build_id_file, version, build_id, daemon_user_root, daemon_id,
            timeout_seconds,
        ).squad_run(root, squad_id, instance, cwd, task_id, task, permission_mode)
    except HaCliError as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        raise typer.Exit(1) from exc
    typer.echo(receipt.model_dump_json(by_alias=True))


@ha_app.command("squad-status")
def ha_squad_status(
    root: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    executable: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    cli_entry: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    build_id_file: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    run_id: Annotated[str, typer.Option("--run-id")],
    daemon_user_root: Annotated[
        Path | None, typer.Option(exists=True, file_okay=False, resolve_path=True)
    ] = None,
    daemon_id: Annotated[str, typer.Option()] = "default",
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=0.001, max=300)] = 30.0,
    interval_seconds: Annotated[float, typer.Option("--interval-seconds", min=0, max=60)] = 1.0,
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=1000)] = 30,
    deadline_seconds: Annotated[float, typer.Option("--deadline-seconds", min=0.001, max=300)] = 30.0,
    version: Annotated[str, typer.Option()] = DEFAULT_HA_CLI_VERSION,
    build_id: Annotated[str, typer.Option()] = DEFAULT_HA_CLI_BUILD_ID,
) -> None:
    """Poll only squad status and emit a bounded JSON receipt."""
    try:
        receipt = _adapter(
            executable, cli_entry, build_id_file, version, build_id, daemon_user_root, daemon_id,
            timeout_seconds,
        ).poll_squad_status(
            root, run_id, interval_seconds=interval_seconds, max_attempts=max_attempts,
            deadline_seconds=deadline_seconds,
        )
    except HaCliError as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        raise typer.Exit(1) from exc
    typer.echo(receipt.model_dump_json(by_alias=True))


@ha_app.command("squad-diagnose")
def ha_squad_diagnose(
    root: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    executable: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    cli_entry: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    build_id_file: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    ledger_path: Annotated[Path, typer.Option("--ledger", dir_okay=False, resolve_path=True)],
    diagnosis_id: Annotated[str, typer.Option("--diagnosis-id")],
    squad_id: Annotated[str, typer.Option("--squad-id")],
    instance: Annotated[str, typer.Option("--instance")],
    cwd: Annotated[str, typer.Option("--cwd")],
    task_id: Annotated[str, typer.Option("--task")],
    prompt: Annotated[str, typer.Option("--prompt")],
    base_commit: Annotated[str, typer.Option("--base-commit")],
    strategy_version: Annotated[str, typer.Option("--strategy-version")],
    model_version: Annotated[str, typer.Option("--model-version")],
    prompt_version: Annotated[str, typer.Option("--prompt-version")],
    daemon_user_root: Annotated[
        Path | None, typer.Option(exists=True, file_okay=False, resolve_path=True)
    ] = None,
    daemon_id: Annotated[str, typer.Option()] = "default",
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=0.001, max=300)] = 30.0,
    interval_seconds: Annotated[float, typer.Option("--interval-seconds", min=0, max=60)] = 1.0,
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=1000)] = 30,
    deadline_seconds: Annotated[float, typer.Option("--deadline-seconds", min=0.001, max=300)] = 30.0,
    permission_mode: Annotated[HaCliPermissionMode, typer.Option("--permission-mode")] = "read-only",
    version: Annotated[str, typer.Option()] = DEFAULT_HA_CLI_VERSION,
    build_id: Annotated[str, typer.Option()] = DEFAULT_HA_CLI_BUILD_ID,
) -> None:
    """Collect one bounded Squad diagnosis and persist its Attempt handoff."""

    try:
        diagnosis = collect_squad_diagnosis(
            _adapter(
                executable, cli_entry, build_id_file, version, build_id, daemon_user_root, daemon_id,
                timeout_seconds,
            ),
            root,
            diagnosis_id=diagnosis_id,
            squad_id=squad_id,
            instance=instance,
            cwd=cwd,
            task_id=task_id,
            prompt=prompt,
            interval_seconds=interval_seconds,
            max_attempts=max_attempts,
            deadline_seconds=deadline_seconds,
            permission_mode=permission_mode,
        )
        attempt = record_squad_diagnosis_attempt(
            Ledger(ledger_path),
            diagnosis,
            base_commit=base_commit,
            strategy_version=strategy_version,
            model_version=model_version,
            prompt_version=prompt_version,
        )
    except (HaCliError, HaDiagnosisError, LedgerConflictError, ValueError) as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {
                "schema": "ha-diagnosis-attempt-receipt/v1",
                "diagnosis": diagnosis.model_dump(mode="json", by_alias=True),
                "attempt": attempt.model_dump(mode="json"),
            },
            sort_keys=True,
        )
    )


@target_app.command("publish")
def target_publish(
    root: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    git_executable: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    git_sha256: Annotated[str, typer.Option()],
    gh_executable: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    gh_sha256: Annotated[str, typer.Option()],
    github_home: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    admission_file: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    title: Annotated[str | None, typer.Option()] = None,
    body_file: Annotated[Path | None, typer.Option(exists=True, dir_okay=False, resolve_path=True)] = None,
    ledger_path: Annotated[Path | None, typer.Option("--ledger", dir_okay=False, resolve_path=True)] = None,
    max_poll_attempts: Annotated[int, typer.Option(min=1, max=120)] = 30,
    poll_interval_seconds: Annotated[float, typer.Option(min=0, max=60)] = 5.0,
) -> None:
    """Create or reuse an accepted target PR, then collect required CI checks.

    This command never pushes protected main, approves a review, or merges a PR.
    Its non-success JSON receipt is intentional evidence and returns exit code 1.
    """

    try:
        if title is None or body_file is None:
            raise TargetPublicationError("--title and --body-file are required")
        admission = CandidateAcceptanceAdmission.model_validate_json(admission_file.read_text(encoding="utf-8"))
        request = TargetPublicationRequest(
            admission=admission,
            title=title,
            body=body_file.read_text(encoding="utf-8"),
            max_poll_attempts=max_poll_attempts,
            poll_interval_seconds=poll_interval_seconds,
        )
        publisher = TargetPublisher(
            TargetPublicationConfig(
                git_executable=git_executable,
                git_sha256=git_sha256,
                gh_executable=gh_executable,
                gh_sha256=gh_sha256,
                github_home=github_home,
            )
        )
        receipt = publisher.publish(root, request, ledger=Ledger(ledger_path) if ledger_path else None)
    except (OSError, TargetPublicationError, ValueError, LedgerConflictError) as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        raise typer.Exit(1) from exc
    typer.echo(receipt.model_dump_json(by_alias=True))
    if receipt.status is not TargetPublicationStatus.SUCCEEDED:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
