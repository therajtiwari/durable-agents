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

    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self.responses = responses
        self.call_count = 0

    async def call(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMResponse:
        response = self.responses[self.call_count]
        self.call_count += 1
        if isinstance(response, Exception):
            raise response
        return response
