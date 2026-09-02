from dataclasses import dataclass

from durable_agents.events import GuardrailAction
from durable_agents.guardrails.types import GuardMatch


@dataclass(frozen=True)
class GuardrailProfile:
    """One named strictness level. Same detection code runs regardless
    of profile (guardrails/patterns.py, output_validate.py, run_level.py
    don't change) — only these thresholds do. See
    docs/THREAT_MODEL.md's "Guardrail profiles" section for the
    reasoning behind each starting number; none of it is final, it's
    meant to be tuned against the attack corpus once one exists.
    """

    name: str
    policy_caps: dict[str, dict[str, float]]
    loop_threshold: int
    escalation_threshold: int
    always_escalate_on_injection: bool
    injection_block_confidence: float | None
    """An injection match at or above this confidence -> BLOCK. None
    means this profile never blocks on pattern confidence alone (it
    still relies on L3's deterministic checks to catch what got through).
    """
    injection_redact_confidence: float
    """An injection match below injection_block_confidence but at or
    above this -> REDACT. Below this -> ALLOW (logged, run continues
    unaffected) — a low-confidence match like a bare "act as" shouldn't
    cost every run a redaction.
    """


PROFILES: dict[str, GuardrailProfile] = {
    "strict": GuardrailProfile(
        name="strict",
        policy_caps={"issue_refund": {"amount_inr": 25_000}},
        loop_threshold=3,
        escalation_threshold=1,
        always_escalate_on_injection=True,
        injection_block_confidence=None,
        injection_redact_confidence=0.0,
    ),
    "standard": GuardrailProfile(
        name="standard",
        policy_caps={"issue_refund": {"amount_inr": 100_000}},
        loop_threshold=3,
        escalation_threshold=3,
        always_escalate_on_injection=False,
        injection_block_confidence=0.85,
        injection_redact_confidence=0.0,
    ),
    "lenient": GuardrailProfile(
        name="lenient",
        policy_caps={"issue_refund": {"amount_inr": 500_000}},
        loop_threshold=3,
        escalation_threshold=5,
        always_escalate_on_injection=False,
        injection_block_confidence=None,
        injection_redact_confidence=0.85,
    ),
}

# financial_v1 is what every existing test fixture's RunStarted.guardrail_profile
# already carries — treated as at least as strict as "standard", since
# refunds are exactly the domain this project's worked example is about.
PROFILE_ALIASES: dict[str, str] = {"financial_v1": "standard"}


def get_profile(guardrail_profile: str | None) -> GuardrailProfile:
    name = PROFILE_ALIASES.get(guardrail_profile or "", guardrail_profile or "standard")
    return PROFILES.get(name, PROFILES["standard"])


def decide(match: GuardMatch, profile: GuardrailProfile) -> GuardrailAction:
    """Map one detection to one action. Pure — same match and profile in,
    same action out, no I/O, matching decide_next_action's own style.

    Deterministic violations (bad schema, unregistered tool, a policy
    number actually exceeded, a real repeated loop) BLOCK under every
    profile: these aren't probabilistic guesses that a stricter setting
    should be more suspicious of, they're facts. Confidence-based
    injection detection is the only place strictness actually changes
    the outcome — that's the whole point of a profile knob.
    """

    if match.rule in ("schema_invalid", "allowlist_violation", "policy_bounds_exceeded", "loop_detected"):
        return "BLOCK"

    if match.rule == "escalation_threshold_reached":
        return "ESCALATE"

    if match.rule.startswith("pii_"):
        return "REDACT"

    if match.rule.startswith("injection_"):
        if profile.always_escalate_on_injection:
            return "ESCALATE"
        if profile.injection_block_confidence is not None and match.confidence >= profile.injection_block_confidence:
            return "BLOCK"
        if match.confidence >= profile.injection_redact_confidence:
            return "REDACT"
        return "ALLOW"

    return "ALLOW"
