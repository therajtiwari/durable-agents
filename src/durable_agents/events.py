import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator


def hash_system_prompt(system_prompt: str) -> str:
    """Stable fingerprint for a system prompt, so "were these two runs
    using the same instructions?" is a cheap comparison instead of a
    full-text diff.
    """

    return "sha256:" + hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()

GuardrailLayer = Literal["L1_input", "L2_tool_result", "L3_output", "L4_run_level"]
GuardrailAction = Literal["ALLOW", "REDACT", "ESCALATE", "BLOCK"]
RunFailureReason = Literal[
    "max_steps_exceeded", "max_cost_exceeded", "guardrail_block", "unrecoverable_error"
]


class BaseEvent(BaseModel):
    seq: int
    created_at: datetime


class ToolCallInvocation(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class RunStarted(BaseEvent):
    type: Literal["RunStarted"] = "RunStarted"
    goal: str
    model: str
    system_prompt: str = ""
    """The instructions the agent runs under. Stored in full, not just
    hashed: a replay that can't reproduce what the model was actually
    told isn't a replay of anything. Defaults empty so events written
    before this field existed still load.
    """
    system_prompt_hash: str = ""
    max_steps: int
    max_cost_usd: Decimal
    requested_by: str
    guardrail_profile: str

    @model_validator(mode="before")
    @classmethod
    def _derive_prompt_hash(cls, data: Any) -> Any:
        # Derived rather than supplied, so the hash and the prompt can
        # never disagree. Only fills a hash that isn't already present,
        # which keeps rows written before system_prompt existed loading
        # with the hash they were actually stored with.
        if isinstance(data, dict) and not data.get("system_prompt_hash"):
            data = {**data, "system_prompt_hash": hash_system_prompt(data.get("system_prompt") or "")}
        return data


class LLMCallRequested(BaseEvent):
    type: Literal["LLMCallRequested"] = "LLMCallRequested"
    step: int
    message_count: int
    estimated_tokens: int


class LLMCallCompleted(BaseEvent):
    type: Literal["LLMCallCompleted"] = "LLMCallCompleted"
    step: int
    content: str | None
    tool_calls: list[ToolCallInvocation]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int
    provider_request_id: str


class LLMCallFailed(BaseEvent):
    type: Literal["LLMCallFailed"] = "LLMCallFailed"
    step: int
    error: str
    attempt: int


class ToolCallRequested(BaseEvent):
    type: Literal["ToolCallRequested"] = "ToolCallRequested"
    step: int
    tool: str
    arguments: dict[str, Any]
    idempotency_key: str
    requires_approval: bool
    approved_by_seq: int | None = None
    tool_call_id: str = ""
    """Which of the model's own tool_calls entries this answers.

    A model may ask for several tools in one response; each gets its own
    Requested/Completed pair, and this is what pairs them back up — both
    for deciding what still needs running and for rebuilding the
    provider's required tool_call_id on the next request. Defaults empty
    so events written before parallel tool calls were supported still
    load; a log from then only ever had one call outstanding at a time,
    so position alone is enough to interpret it.
    """


class ToolCallCompleted(BaseEvent):
    type: Literal["ToolCallCompleted"] = "ToolCallCompleted"
    step: int
    tool: str
    idempotency_key: str
    result: dict[str, Any]
    duration_ms: int
    recovered: bool
    provider_dedup_hit: bool
    tool_call_id: str = ""


class ToolCallFailed(BaseEvent):
    type: Literal["ToolCallFailed"] = "ToolCallFailed"
    step: int
    tool: str
    arguments: dict[str, Any]
    idempotency_key: str
    error: str
    attempt: int
    tool_call_id: str = ""
    final_attempt: bool = True
    """False when the orchestrator intends to retry this exact call again
    (same idempotency_key), so the operation stays in flight. True when
    the attempt budget is spent and the error should be surfaced to the
    model instead. Defaults True so events written before retries
    existed — all of which were terminal unknown-tool failures — still
    rebuild correctly.
    """


class GuardrailTriggered(BaseEvent):
    type: Literal["GuardrailTriggered"] = "GuardrailTriggered"
    layer: GuardrailLayer
    rule: str
    action: GuardrailAction
    step: int | None
    detail: dict[str, Any]
    latency_ms: int


class ApprovalRequested(BaseEvent):
    type: Literal["ApprovalRequested"] = "ApprovalRequested"
    step: int
    tool: str
    arguments: dict[str, Any]
    reason: str
    tool_call_id: str = ""
    """Which specific tool call a human is being asked about. Carried so
    a grant releases exactly that call — when a model asks for several
    tools at once and only one of them needs approving, approving it
    must not silently clear the gate for the others.
    """


class ApprovalGranted(BaseEvent):
    type: Literal["ApprovalGranted"] = "ApprovalGranted"
    approver: str


class ApprovalDenied(BaseEvent):
    type: Literal["ApprovalDenied"] = "ApprovalDenied"
    approver: str
    reason: str


class RunCompleted(BaseEvent):
    type: Literal["RunCompleted"] = "RunCompleted"
    final_answer: str
    total_steps: int
    total_tokens: int
    total_cost_usd: Decimal


class RunFailed(BaseEvent):
    type: Literal["RunFailed"] = "RunFailed"
    reason: RunFailureReason
    detail: str | None


Event = Annotated[
    RunStarted
    | LLMCallRequested
    | LLMCallCompleted
    | LLMCallFailed
    | ToolCallRequested
    | ToolCallCompleted
    | ToolCallFailed
    | GuardrailTriggered
    | ApprovalRequested
    | ApprovalGranted
    | ApprovalDenied
    | RunCompleted
    | RunFailed,
    Field(discriminator="type"),
]
