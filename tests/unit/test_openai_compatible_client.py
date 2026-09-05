import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from durable_agents.events import ToolCallInvocation
from durable_agents.llm.openai_compatible import OpenAICompatibleClient
from durable_agents.state import Message


def _client(handler: Any, **kwargs: Any) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="https://example.invalid/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _json_response(body: dict[str, Any], status_code: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return handler


def _text_response() -> dict[str, Any]:
    return {
        "id": "chatcmpl-123",
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello there."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }


@pytest.mark.asyncio
async def test_plain_text_response_is_parsed() -> None:
    client = _client(_json_response(_text_response()))
    response = await client.call([Message(role="user", content="hi")], tools=[])

    assert response.content == "Hello there."
    assert response.stop_reason == "stop"
    assert response.input_tokens == 10
    assert response.output_tokens == 4
    assert response.provider_request_id == "chatcmpl-123"
    assert response.tool_calls == []
    await client.aclose()


@pytest.mark.asyncio
async def test_tool_call_response_is_parsed() -> None:
    body = {
        "id": "chatcmpl-456",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "issue_refund",
                                "arguments": json.dumps({"amount": 400}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8},
    }
    client = _client(_json_response(body))
    response = await client.call([Message(role="user", content="refund it")], tools=[])

    assert response.tool_calls == [
        ToolCallInvocation(id="call_abc", name="issue_refund", arguments={"amount": 400})
    ]
    assert response.stop_reason == "tool_calls"
    await client.aclose()


@pytest.mark.asyncio
async def test_cost_is_computed_from_configured_rates() -> None:
    body = _text_response()  # 10 input, 4 output tokens
    client = _client(
        _json_response(body),
        cost_per_1k_input_tokens="3.00",
        cost_per_1k_output_tokens="15.00",
    )
    response = await client.call([Message(role="user", content="hi")], tools=[])

    # 10/1000 * 3.00 + 4/1000 * 15.00 = 0.03 + 0.06 = 0.09
    assert response.cost_usd == Decimal("0.09")
    await client.aclose()


@pytest.mark.asyncio
async def test_cost_defaults_to_zero_when_rates_not_configured() -> None:
    client = _client(_json_response(_text_response()))
    response = await client.call([Message(role="user", content="hi")], tools=[])
    assert response.cost_usd == Decimal("0")
    await client.aclose()


@pytest.mark.asyncio
async def test_request_shape_translates_messages_tools_and_system_prompt() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_text_response())

    client = _client(handler, api_key="sk-test")
    messages = [
        Message(role="user", content="Refund order A-8891."),
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCallInvocation(id="call_1", name="issue_refund", arguments={"amount": 400})],
        ),
        Message(role="tool", content='{"refund_id": "RF-1"}', tool_name="issue_refund"),
    ]
    tools = [
        {
            "name": "issue_refund",
            "description": "Refund an order.",
            "parameters": {"type": "object", "properties": {"amount": {"type": "integer"}}},
        }
    ]

    await client.call(messages, tools, system_prompt="Be careful with refunds.")

    payload = captured["payload"]
    assert payload["model"] == "test-model"
    assert captured["auth"] == "Bearer sk-test"

    sent = payload["messages"]
    assert sent[0] == {"role": "system", "content": "Be careful with refunds."}
    assert sent[1] == {"role": "user", "content": "Refund order A-8891."}
    assert sent[2]["role"] == "assistant"
    assert sent[2]["tool_calls"][0]["id"] == "call_1"
    assert sent[2]["tool_calls"][0]["function"]["name"] == "issue_refund"
    assert json.loads(sent[2]["tool_calls"][0]["function"]["arguments"]) == {"amount": 400}
    # The tool-role message must carry the SAME id the assistant's
    # tool_calls entry used — this is what OpenAI's wire format requires
    # to correlate a tool result with the call it answers, and it's
    # recovered here since durable_agents never persists it separately.
    assert sent[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"refund_id": "RF-1"}',
    }

    sent_tool_schema = payload["tools"][0]
    assert sent_tool_schema["type"] == "function"
    assert sent_tool_schema["function"]["name"] == "issue_refund"
    assert sent_tool_schema["function"]["parameters"] == tools[0]["parameters"]
    await client.aclose()


@pytest.mark.asyncio
async def test_request_url_is_not_mangled_by_base_url_merging() -> None:
    """Regression: httpx merges a client's base_url with a relative
    request path by raw concatenation, with NO separator inserted. A
    base_url without a trailing slash ("https://host/v1", the natural
    way anyone writes one) plus a request path with a leading slash
    ("/chat/completions") silently produced "https://host/v1chat/
    completions" — a real 404 against Groq's live API that no earlier
    test caught, because none of them asserted on the request URL
    itself, only on the body and headers. A mock doesn't care what path
    it was asked for; a real server does.
    """

    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(200, json=_text_response())

    # Deliberately no trailing slash — the exact shape that broke.
    client = OpenAICompatibleClient(
        base_url="https://api.groq.com/openai/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    await client.call([Message(role="user", content="hi")], tools=[])
    await client.aclose()

    assert captured_urls == ["https://api.groq.com/openai/v1/chat/completions"]


@pytest.mark.asyncio
async def test_http_error_status_raises_instead_of_swallowing() -> None:
    """Orchestrator's own retry/backoff is what should
    react to a failure — this client must not swallow one itself, or
    the retry budget in the event log would never see it.
    """

    client = _client(_json_response({"error": "rate limited"}, status_code=429))
    with pytest.raises(httpx.HTTPStatusError):
        await client.call([Message(role="user", content="hi")], tools=[])
    await client.aclose()
