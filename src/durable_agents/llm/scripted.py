from collections.abc import Sequence
from typing import Any

from durable_agents.llm.protocol import LLMClient, LLMResponse
from durable_agents.state import Message


class ScriptedLLM(LLMClient):
    """A fake LLMClient that replays a fixed, pre-baked list of responses.

    No network call, no randomness, no cost — used to test the orchestrator
    without depending on a real (slow, non-deterministic, paid) provider.
    Each call returns the next item in the script; raises if it's an
    Exception instead of an LLMResponse, so a scripted failure can be
    tested the same way a real provider error would be.
    """

    def __init__(self, responses: Sequence[LLMResponse | Exception]) -> None:
        # Sequence, not list: list is invariant, so a caller passing a
        # plain list[LLMResponse] (the common case — no failures being
        # simulated) would otherwise fail type checking and have to
        # annotate the union by hand.
        self.responses = list(responses)
        self.call_count = 0
        self.last_system_prompt: str = ""

    async def call(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        system_prompt: str = "",
    ) -> LLMResponse:
        # Recorded rather than used — a scripted response ignores its
        # input by definition, but a test asserting the orchestrator
        # actually forwards the prompt needs somewhere to look.
        self.last_system_prompt = system_prompt
        response = self.responses[self.call_count]
        self.call_count += 1
        if isinstance(response, Exception):
            raise response
        return response
