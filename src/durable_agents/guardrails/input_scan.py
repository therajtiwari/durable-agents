from collections.abc import Sequence

from durable_agents.guardrails.patterns import (
    DEFAULT_PII_PATTERNS,
    PIIPattern,
    scan_patterns,
    scan_pii,
)
from durable_agents.guardrails.types import ScanResult


async def scan_input(
    goal: str, pii_patterns: Sequence[PIIPattern] = DEFAULT_PII_PATTERNS
) -> ScanResult:
    """L1 — runs once on the user's goal, before the first LLM call.

    async to match LLMClient's own calling convention and leave room for
    a real classifier check later (Week 6, needs AnthropicClient) without
    a breaking signature change — nothing here actually awaits anything
    yet.
    """

    injection_matches = scan_patterns(goal)
    pii_matches, redacted_text = scan_pii(goal, pii_patterns)
    return ScanResult(matches=[*injection_matches, *pii_matches], redacted_text=redacted_text)
