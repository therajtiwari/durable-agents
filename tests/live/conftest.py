"""Shared setup for tests/live — the tier that hits a real LLM provider.

Everything here reads from environment variables rather than hardcoding
a provider: OpenAICompatibleClient is meant to work against any
OpenAI-compatible endpoint (Groq, OpenAI itself, a local Ollama/vLLM
server, OpenRouter, ...), and baking one vendor's env var name into the
test suite would be the same bias this client itself was built to avoid
(see DECISIONS.md's "Provider client" section).
"""

import os
from collections.abc import AsyncGenerator

import pytest_asyncio

from durable_agents.llm.openai_compatible import OpenAICompatibleClient

LIVE_API_KEY = os.environ.get("LLM_API_KEY")
LIVE_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
# Verified against Groq's free tier. Provider model names churn —
# override with LLM_MODEL if this one has since been retired.
LIVE_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")


@pytest_asyncio.fixture
async def live_llm() -> AsyncGenerator[OpenAICompatibleClient, None]:
    client = OpenAICompatibleClient(base_url=LIVE_BASE_URL, model=LIVE_MODEL, api_key=LIVE_API_KEY)
    try:
        yield client
    finally:
        await client.aclose()
