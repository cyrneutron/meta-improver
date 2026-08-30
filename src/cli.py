"""Small command-line entry point for controlled provider diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from src.ha_cli import HaCliAdapter, HaCliConfig, HaCliError


app = typer.Typer(help="Meta-Improver proposal-only control surface.", no_args_is_help=True)
ha_app = typer.Typer(help="Inspect the pinned Harness Anything provider.", no_args_is_help=True)
app.add_typer(ha_app, name="ha")


def _adapter(executable: Path, cli_entry: Path, build_id_file: Path, version: str, build_id: str) -> HaCliAdapter:
    return HaCliAdapter(HaCliConfig(
        executable=executable, cli_entry=cli_entry, build_id_file=build_id_file,
        expected_version=version, expected_build_id=build_id,
    ))


@ha_app.command("check")
def ha_check(
    root: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    executable: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    cli_entry: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    build_id_file: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    version: Annotated[str, typer.Option()] = "0.1.0",
    build_id: Annotated[str, typer.Option()] = "5fa644bc-4a6c-44a0-8a7d-f7897a1877a6",
) -> None:
    """Verify version/build identity and print capabilities as JSON."""
    try:
        receipt = _adapter(executable, cli_entry, build_id_file, version, build_id).capabilities(root)
    except HaCliError as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        raise typer.Exit(1) from exc
    typer.echo(receipt.model_dump_json(by_alias=True))


if __name__ == "__main__":
    app()
