from dataclasses import dataclass

from durable_agents.events import GuardrailAction
from durable_agents.guardrails.types import GuardMatch


@dataclass(frozen=True)
class GuardrailProfile:
    """One named guardrail configuration.

    These cover two genuinely different kinds of check, and the switches
    below exist because they have opposite characteristics:

    **Deterministic checks** — does this tool exist, do the arguments
    match its declared schema, is this number over a configured cap, is
    the agent repeating one side-effecting action. These have no false
    positives by construction. They are not really a security opinion,
    they are argument validation, and every profile except "off" runs
    them.

    **Pattern matching** — regexes guessing whether some text is trying
    to manipulate the model. This project's own eval measures a 20%
    false positive rate, and a false positive here means a BLOCK, i.e. a
    dead run. Ordinary machine output trips it: a tool returning
    {"error": "system: disk full"} matches injection_system_override at
    0.9 confidence. Since L2 scans every tool result, any agent that
    reads logs, tickets or error messages runs into this. It is off in
    the default profile and opted into by name.

    See docs/THREAT_MODEL.md for the measured numbers and the reasoning
    behind each threshold; none of it is final.
    """

    name: str
    deterministic_checks: bool
    """L3 schema/allowlist/policy-cap validation and L4 loop and
    escalation detection. Zero false positives; on everywhere but "off".
    """
    pii_detection: bool
    """Scan for PII, record that it was found (never the value — see
    patterns.scan_pii), and redact it before anything reaches the
    provider.
    """
    injection_patterns: bool
    """The 20%-false-positive layer. Off by default, on by name."""
    delimit_tool_results: bool
    """Wrap every tool result in explicit untrusted-data markers before
    the model sees it. Structural, not a detection: it cannot produce a
    false positive because it never decides anything.
    """
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

    def considers(self, rule: str) -> bool:
        """Whether a detection of this kind is acted on at all.

        Applied before anything is appended, so a layer this profile has
        switched off produces no GuardrailTriggered events rather than a
        run of ALLOWs — an audit trail should record checks that were
        actually made.
        """

        if rule.startswith("injection_"):
            return self.injection_patterns
        if rule.startswith("pii_"):
            return self.pii_detection
        return self.deterministic_checks


DEFAULT_PROFILE = "validation"
"""What a run gets when nothing asks for anything else.

Deliberately not one of the injection-scanning profiles. A durability
runtime whose default kills one run in five over a regex would be
undermining the thing it is actually good at, and a first-time user
would reasonably conclude the durable-execution part was broken. The
pattern layer is real work and stays available — it is opted into by
name, by someone who has read what it costs.
"""


def _profile(
    name: str,
    *,
    deterministic_checks: bool = True,
    pii_detection: bool = True,
    injection_patterns: bool = True,
    delimit_tool_results: bool = True,
    policy_caps: dict[str, dict[str, float]] | None = None,
    loop_threshold: int = 3,
    escalation_threshold: int = 3,
    always_escalate_on_injection: bool = False,
    injection_block_confidence: float | None = None,
    injection_redact_confidence: float = 0.0,
) -> GuardrailProfile:
    return GuardrailProfile(
        name=name,
        deterministic_checks=deterministic_checks,
        pii_detection=pii_detection,
        injection_patterns=injection_patterns,
        delimit_tool_results=delimit_tool_results,
        # Known limitation: the shipped profiles below carry example caps
        # (issue_refund/amount_inr) that mean nothing to another
        # consumer. They stay until policy caps have a real configuration
        # path — emptying them here would silently remove a documented
        # check rather than make it configurable.
        policy_caps=policy_caps if policy_caps is not None else {},
        loop_threshold=loop_threshold,
        escalation_threshold=escalation_threshold,
        always_escalate_on_injection=always_escalate_on_injection,
        injection_block_confidence=injection_block_confidence,
        injection_redact_confidence=injection_redact_confidence,
    )


PROFILES: dict[str, GuardrailProfile] = {
    # Nothing at all: no checks, no redaction, no delimiting. For a
    # consumer who wants durable execution and no opinions about their
    # prompts or their data.
    "off": _profile(
        "off",
        deterministic_checks=False,
        pii_detection=False,
        injection_patterns=False,
        delimit_tool_results=False,
    ),
    # The default. Everything that cannot produce a false positive.
    "validation": _profile(
        "validation",
        injection_patterns=False,
        policy_caps={"issue_refund": {"amount_inr": 100_000}},
    ),
    "lenient": _profile(
        "lenient",
        policy_caps={"issue_refund": {"amount_inr": 500_000}},
        escalation_threshold=5,
        injection_redact_confidence=0.85,
    ),
    "standard": _profile(
        "standard",
        policy_caps={"issue_refund": {"amount_inr": 100_000}},
        injection_block_confidence=0.85,
    ),
    "strict": _profile(
        "strict",
        policy_caps={"issue_refund": {"amount_inr": 25_000}},
        escalation_threshold=1,
        always_escalate_on_injection=True,
    ),
}

# financial_v1 predates the named profiles and is what this project's own
# refund fixtures carry — kept resolving rather than becoming an error,
# since an alias that already appears in event logs cannot be withdrawn
# without making those runs unresumable.
PROFILE_ALIASES: dict[str, str] = {"financial_v1": "standard"}


def get_profile(guardrail_profile: str | None) -> GuardrailProfile:
    """Resolve a profile name, or raise if it isn't one.

    Raises rather than falling back, which is what it used to do: a
    typo'd "strct" silently resolved to standard, so a deployment could
    believe it was running strict for months. A name nobody recognises
    is a configuration bug, and configuration bugs in a safety layer
    should be loud.
    """

    if guardrail_profile is None or guardrail_profile == "":
        return PROFILES[DEFAULT_PROFILE]

    name = PROFILE_ALIASES.get(guardrail_profile, guardrail_profile)
    profile = PROFILES.get(name)
    if profile is None:
        valid = ", ".join(sorted(PROFILES) + sorted(PROFILE_ALIASES))
        raise ValueError(f"unknown guardrail profile {guardrail_profile!r}; valid names: {valid}")
    return profile


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
