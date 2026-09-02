from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from durable_agents.events import ToolCallInvocation
from durable_agents.state import Message


class LLMResponse(BaseModel):
    content: str | None
    tool_calls: list[ToolCallInvocation]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int
    provider_request_id: str


class LLMClient(ABC):
    @abstractmethod
    async def call(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> LLMResponse:
        """Send messages + tool schemas to the model, return its response.

        tools is a neutral shape — [{"name", "description", "parameters":
        <json schema>}, ...] — not any one provider's wire format and
        not the tool registry's own Tool type. Translating that into
        whatever your provider actually expects (Anthropic wants
        input_schema; OpenAI wants it wrapped in {"type": "function",
        "function": {...}}) is this method's job.

        system_prompt arrives as a separate argument rather than as a
        message because providers model it separately on the wire, and
        because it is a property of the run (recorded once on
        RunStarted) rather than a turn in the conversation.
        """
        raise NotImplementedError
