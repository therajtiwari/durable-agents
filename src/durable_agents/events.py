from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

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
    system_prompt_hash: str
    max_steps: int
    max_cost_usd: Decimal
    requested_by: str
    guardrail_profile: str


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


class ToolCallCompleted(BaseEvent):
    type: Literal["ToolCallCompleted"] = "ToolCallCompleted"
    step: int
    tool: str
    idempotency_key: str
    result: dict[str, Any]
    duration_ms: int
    recovered: bool
    provider_dedup_hit: bool


class ToolCallFailed(BaseEvent):
    type: Literal["ToolCallFailed"] = "ToolCallFailed"
    step: int
    tool: str
    arguments: dict[str, Any]
    idempotency_key: str
    error: str
    attempt: int


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
