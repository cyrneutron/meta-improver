"""Minimal proposal-only orchestration plan over the controlled HA adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.ha_cli import HaCliAdapter, HaCliStatus
from src.proposal import ProposalPlan, rehydrate_proposal_plan


class ProposalOrchestrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["proposal-orchestration-plan/v1"] = Field(
        default="proposal-orchestration-plan/v1", alias="schema", serialization_alias="schema"
    )
    mode: Literal["proposal_only"] = "proposal_only"
    status: Literal["ready", "unsupported"]
    proposal: ProposalPlan
    provider_version: str
    provider_build_id: str
    squad_run_supported: bool
    reason: str


class ProposalOrchestrator:
    """Prove provider compatibility without starting work or publishing anything."""

    def __init__(self, adapter: HaCliAdapter) -> None:
        self.adapter = adapter

    def plan(self, root: Path, proposal: ProposalPlan) -> ProposalOrchestrationPlan:
        hydrated = rehydrate_proposal_plan(proposal)
        capabilities = self.adapter.capabilities(root)
        payload = capabilities.receipt or {}
        squad = payload.get("squad")
        supported = capabilities.status is HaCliStatus.SUCCEEDED and isinstance(squad, list) and "squad-run" in squad
        return ProposalOrchestrationPlan(
            status="ready" if supported else "unsupported",
            proposal=hydrated,
            provider_version=capabilities.provider_version,
            provider_build_id=capabilities.provider_build_id,
            squad_run_supported=supported,
            reason="contracted squad-run capability is available" if supported else "squad-run capability is not confirmed",
        )
