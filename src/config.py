"""Validated runtime configuration for the proposal engine.

This module intentionally contains no provider-specific credentials. Secrets are
referenced by name and must be resolved by a later, explicitly approved layer.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ImproverConfig(BaseModel):
    """Configuration shared by ingestion, proposal, and ledger components."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_root: Path = Field(default_factory=Path.cwd)
    ledger_path: Path = Field(default=Path(".improver_history/history.db"))
    model_name: str = Field(default="", max_length=200)
    prompt_version: str = Field(default="v1", min_length=1, max_length=100)
    max_input_chars: int = Field(default=20_000, ge=1, le=1_000_000)
    max_patch_files: int = Field(default=50, ge=1, le=1_000)
    max_patch_chars: int = Field(default=500_000, ge=1, le=10_000_000)
    allow_network: bool = False
    secret_ref: str | None = Field(default=None, max_length=200)

    @field_validator("project_root", "ledger_path", mode="before")
    @classmethod
    def non_empty_path(cls, value: Path | str) -> Path:
        path = Path(value)
        if not str(path):
            raise ValueError("path must not be empty")
        return path

