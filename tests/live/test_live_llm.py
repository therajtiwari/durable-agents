"""Layer 8 — live tests, per docs/SPEC.md's testing strategy: a real
provider, fake side effects always, skipped cleanly without a key so a
fresh clone still runs green on the ordinary (non-live) test suite.

    pytest -m live tests/live          # explicit opt-in only
    $env:LLM_API_KEY = "gsk_..."       # a Groq key works for free

Kept to two tests on purpose. This tier exists to catch "does the wire
format actually work against a real server" — exactly the class of bug
the URL-merging fix in Iteration 26 was (a mocked transport can't catch
a malformed request path; only a real server returns a real 404 for
it). It is not a place to re-prove orchestrator logic already covered
for free by the scripted suite.

Assert on invariants, not exact wording: a real model may phrase its
answer differently between runs, or take an extra step. "It got the
right sum" is a fact worth asserting; "it said this exact sentence" is
not.
"""

from decimal import Decimal
from typing import Any

import pytest

from durable_agents import InMemoryEventStore, Runtime, tool
from durable_agents.llm.openai_compatible import OpenAICompatibleClient
from durable_agents.llm.protocol import LLMResponse
from durable_agents.state import Message

from conftest import LIVE_API_KEY, LIVE_BASE_URL, LIVE_MODEL

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not LIVE_API_KEY,
        reason="set LLM_API_KEY to run live tests against a real provider (see README)",
    ),
]


async def test_live_smoke_plain_completion(live_llm: OpenAICompatibleClient) -> None:
    """The cheapest possible real call: no tools, no runtime, straight
    through the client. If this fails, nothing else in this tier will
    either — the wire format itself is broken, not agent logic on top
    of it.
    """

    response: LLMResponse = await live_llm.call(
        messages=[Message(role="user", content="Reply with exactly one word: OK")],
        tools=[],
    )

    assert response.content is not None
    assert len(response.content.strip()) > 0
    # A real provider fills these in; ScriptedLLM hardcodes them, so
    # a nonzero, provider-issued id is itself evidence this was live.
    assert response.provider_request_id
    assert response.input_tokens > 0


async def test_live_tool_calling_end_to_end() -> None:
    """The whole stack, for real: Runtime -> real HTTP call -> the
    model choosing to invoke a tool -> the runtime executing it ->
    a second real call producing a final answer.
    """

    calls: list[dict[str, Any]] = []

    @tool()
    async def add_numbers(a: int, b: int) -> dict[str, Any]:
        """Add two numbers together and return the sum."""
        calls.append({"a": a, "b": b})
        return {"sum": a + b}

    llm = OpenAICompatibleClient(base_url=LIVE_BASE_URL, model=LIVE_MODEL, api_key=LIVE_API_KEY)
    try:
        runtime = Runtime(
            store=InMemoryEventStore(),
            llm=llm,
            tools=[add_numbers],
            system_prompt="You are a precise calculator. Always use your tool for arithmetic.",
            max_steps=5,
            max_cost_usd=Decimal("0.10"),
        )
        run = await runtime.start(goal="What is 47 plus 89? Use your tool.")
    finally:
        await llm.aclose()

    assert run.state.status == "completed"
    assert len(calls) >= 1, "the model never called the tool at all"
    assert calls[0] == {"a": 47, "b": 89} or calls[0] == {"a": 89, "b": 47}
    assert run.state.final_answer is not None
    assert "136" in run.state.final_answer
