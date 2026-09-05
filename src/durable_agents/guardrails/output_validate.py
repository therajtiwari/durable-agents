import json
from collections.abc import Sequence

from pydantic import ValidationError

from durable_agents.events import ToolCallInvocation
from durable_agents.guardrails.patterns import DEFAULT_PII_PATTERNS, PIIPattern, scan_pii
from durable_agents.guardrails.types import GuardMatch, ScanResult
from durable_agents.tools.registry import Tool


async def validate_output(
    tool_call: ToolCallInvocation,
    tools: dict[str, Tool],
    policy_caps: dict[str, dict[str, float]] | None = None,
    pii_patterns: Sequence[PIIPattern] = DEFAULT_PII_PATTERNS,
) -> ScanResult:
    """L3 — runs on what the model asked for, before any tool executes.

    policy_caps is deliberately a parameter, not a hardcoded constant —
    same reasoning as PII patterns in docs/THREAT_MODEL.md: a refund cap
    is this demo's business rule, not something a generic runtime should
    assume. Shape: {tool_name: {argument_name: max_value}}. A tool or
    argument with no entry is simply not bounds-checked.
    """

    matches: list[GuardMatch] = []

    tool_obj = tools.get(tool_call.name)
    if tool_obj is None:
        # Allowlist violation — the model asked for a tool this run never
        # registered. Distinct from the orchestrator's own
        # hallucinated-tool handling (a non-adversarial ToolCallFailed the
        # model can react to); this is the guardrail's independent record
        # of the same fact, for the case where it wasn't an accident.
        matches.append(
            GuardMatch(rule="allowlist_violation", confidence=1.0, detail={"tool": tool_call.name})
        )
        return ScanResult(matches=matches, redacted_text="")

    try:
        tool_obj.args_model.model_validate(tool_call.arguments)
    except ValidationError as exc:
        matches.append(
            GuardMatch(
                rule="schema_invalid",
                confidence=1.0,
                detail={"tool": tool_call.name, "error": str(exc)},
            )
        )

    caps = (policy_caps or {}).get(tool_call.name, {})
    for field, cap in caps.items():
        value = tool_call.arguments.get(field)
        if isinstance(value, int | float) and value > cap:
            matches.append(
                GuardMatch(
                    rule="policy_bounds_exceeded",
                    confidence=1.0,
                    detail={"tool": tool_call.name, "field": field, "value": value, "cap": cap},
                )
            )

    # Did the model echo something that looks like PII into its own
    # tool-call arguments? Reuses the same PII detector as L1/L2 — the
    # surface differs, the pattern doesn't. json.dumps, not a bare join
    # of the string values: a plain-space join can accidentally weld two
    # unrelated arguments' digits into one run that a PII pattern then
    # matches for real — JSON's own quotes/colons/commas reliably break
    # that adjacency (see the identical fix in orchestrator.py's L2 hook).
    arg_text = json.dumps(tool_call.arguments)
    pii_matches, redacted_text = scan_pii(arg_text, pii_patterns)
    matches.extend(pii_matches)

    return ScanResult(matches=matches, redacted_text=redacted_text)
