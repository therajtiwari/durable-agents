import json
import time
from decimal import Decimal
from typing import Any

import httpx

from durable_agents.events import ToolCallInvocation
from durable_agents.llm.protocol import LLMClient, LLMResponse
from durable_agents.state import Message


class OpenAICompatibleClient(LLMClient):
    """Talks to any provider that speaks the OpenAI chat-completions
    wire format — which by now is most of them: OpenAI itself, Azure
    OpenAI, and the majority of open-source/local model servers (Ollama,
    vLLM, LM Studio, Groq, Together, OpenRouter, and more all expose
    this same shape). One implementation instead of one per vendor SDK.

    A dedicated AnthropicClient can still be added later against the
    same LLMClient interface — this is deliberately not "the" official
    provider, just the one with the broadest reach for a single
    implementation. Point base_url at whichever server you're using.

    Cost is not computed from a hardcoded price table — those vary by
    model, change often, and hardcoding one vendor's prices into a
    generic client is the same mistake as hardcoding one country's PII
    formats into the guardrails. Pass cost_per_1k_input/output_tokens if
    you want cost_usd populated; it defaults to $0 otherwise.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        cost_per_1k_input_tokens: Decimal | float | str = Decimal("0"),
        cost_per_1k_output_tokens: Decimal | float | str = Decimal("0"),
        timeout_seconds: float = 60.0,
        extra_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._cost_per_1k_input = Decimal(str(cost_per_1k_input_tokens))
        self._cost_per_1k_output = Decimal(str(cost_per_1k_output_tokens))
        headers = dict(extra_headers or {})
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # transport is a seam for tests (httpx.MockTransport) — never
        # something a real caller needs to set.
        self._client = httpx.AsyncClient(
            base_url=self._base_url, headers=headers, timeout=timeout_seconds, transport=transport
        )

    async def call(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_openai_messages(messages, system_prompt),
        }
        if tools:
            payload["tools"] = [self._to_openai_tool(t) for t in tools]

        start = time.monotonic()
        # No retry here on purpose: Orchestrator already retries a
        # failed LLM call with backoff (see orchestrator.py's _reconcile),
        # reading the attempt budget back out of the event log. Retrying
        # again at this layer would double the backoff for no benefit.
        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        latency_ms = int((time.monotonic() - start) * 1000)

        body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
        usage = body.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        return LLMResponse(
            content=message.get("content"),
            tool_calls=self._from_openai_tool_calls(message.get("tool_calls") or []),
            stop_reason=choice.get("finish_reason", "unknown"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=(
                self._cost_per_1k_input * input_tokens
                + self._cost_per_1k_output * output_tokens
            )
            / Decimal(1000),
            latency_ms=latency_ms,
            provider_request_id=body.get("id", ""),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _to_openai_messages(
        self, messages: list[Message], system_prompt: str
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})

        # OpenAI's wire format requires every tool-role message to carry
        # tool_call_id, matching the id on the assistant tool_calls entry
        # it responds to. That id lives on the preceding assistant
        # message's own ToolCallInvocation (never persisted onto the
        # ToolCallRequested/Completed events themselves), so it's
        # recovered here by pairing each tool message with the assistant
        # message directly before it — valid because the orchestrator
        # only ever acts on one tool call per step today.
        last_tool_call_id: str | None = None
        for message in messages:
            if message.role == "user":
                result.append({"role": "user", "content": message.content or ""})
            elif message.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": message.content}
                if message.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in message.tool_calls
                    ]
                    last_tool_call_id = message.tool_calls[0].id
                result.append(entry)
            else:  # tool
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": last_tool_call_id or "",
                        "content": message.content or "",
                    }
                )
        return result

    @staticmethod
    def _to_openai_tool(schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["parameters"],
            },
        }

    @staticmethod
    def _from_openai_tool_calls(raw: list[dict[str, Any]]) -> list[ToolCallInvocation]:
        return [
            ToolCallInvocation(
                id=call["id"],
                name=call["function"]["name"],
                arguments=json.loads(call["function"]["arguments"] or "{}"),
            )
            for call in raw
        ]
