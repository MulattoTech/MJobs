"""Small least-privilege default tools for local agent orchestration."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from services.agents.registry import (
    AgentTool, AgentToolContext, PolicyDecision, ToolClass, ToolRegistry,
)
from services.career_profile.profile_access import build_profile_context
from services.tracking.jobs import DISPOSITIONS, set_job_disposition


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ProfileContextInput(_StrictModel):
    requested_paths: list[str] = Field(min_length=1, max_length=20)


class ProfileContextOutput(_StrictModel):
    task: str
    operation_id: str
    profile_version: str
    context: dict[str, Any]
    accessed_fact_ids: list[str]
    accessed_answer_ids: list[str]


class JobDispositionInput(_StrictModel):
    job_id: str = Field(min_length=1, max_length=128)
    disposition: Literal["ACTIVE", "SAVED", "HIDDEN", "NOT_INTERESTED"]


class JobDispositionOutput(_StrictModel):
    disposition: Literal["ACTIVE", "SAVED", "HIDDEN", "NOT_INTERESTED"]


class AgentProposalOutput(_StrictModel):
    proposal_id: str
    tool_name: str
    classification: Literal["SIDE_EFFECT"]
    arguments: dict[str, Any]
    review_required: Literal[True]


def build_default_registry(store) -> ToolRegistry:
    registry = ToolRegistry()

    def read_profile(args: ProfileContextInput, context: AgentToolContext):
        return build_profile_context(store, context.profile_id, context.task,
                                     context.run_id, args.requested_paths)

    registry.register(AgentTool(
        name="read_profile_context",
        description="Read only verified profile fields already allowed for this task. Requested fields fail closed.",
        classification=ToolClass.READ,
        input_model=ProfileContextInput,
        output_model=ProfileContextOutput,
        handler=read_profile,
    ))

    def disposition_policy(args: JobDispositionInput, context: AgentToolContext):
        if args.disposition == "ACTIVE" or args.disposition not in DISPOSITIONS:
            return PolicyDecision(False, "MANUAL_REACTIVATION_REQUIRED")
        try:
            store.profile(context.profile_id)
            store.job(args.job_id)
        except Exception:
            return PolicyDecision(False, "PROFILE_OR_JOB_UNAVAILABLE")
        return PolicyDecision(True, "LOCAL_JOB_TRACKING_ONLY")

    def set_disposition(args: JobDispositionInput, context: AgentToolContext):
        if context.proposal_id is None:
            raise ValueError("A reviewed proposal identifier is required")
        result = set_job_disposition(store, context.profile_id, args.job_id,
                                     args.disposition, context.proposal_id)
        return {"disposition": result["disposition"]}

    registry.register(AgentTool(
        name="set_job_disposition",
        description="Propose a local job-tracking disposition. A matching human review and deterministic policy check are required before writing; this never submits an application.",
        classification=ToolClass.SIDE_EFFECT,
        input_model=JobDispositionInput,
        output_model=AgentProposalOutput,
        handler=set_disposition,
        policy=disposition_policy,
        execution_model=JobDispositionOutput,
    ))
    registry.seal()
    return registry
