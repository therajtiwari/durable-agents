import pytest

from durable_agents.guardrails.decisions import (
    DEFAULT_PROFILE,
    PROFILES,
    decide,
    get_profile,
)
from durable_agents.guardrails.types import GuardMatch


def test_no_profile_named_gets_the_validation_default() -> None:
    """The default runs the checks that cannot be wrong and none of the
    pattern matching — see DEFAULT_PROFILE for why.
    """

    assert get_profile(None) is PROFILES["validation"]
    assert get_profile("") is PROFILES["validation"]
    assert PROFILES[DEFAULT_PROFILE] is PROFILES["validation"]


def test_unknown_profile_raises_instead_of_silently_downgrading() -> None:
    """It used to fall back to standard, so a typo'd "strct" left a
    deployment believing it ran strict. A name nobody recognises is a
    configuration bug in a safety layer, and should be loud.
    """

    with pytest.raises(ValueError) as exc:
        get_profile("strct")

    message = str(exc.value)
    assert "strct" in message
    assert "strict" in message and "validation" in message, "the error must list the valid names"


def test_financial_v1_alias_maps_to_standard() -> None:
    assert get_profile("financial_v1") is PROFILES["standard"]


def test_deterministic_violations_always_block_regardless_of_profile() -> None:
    for rule in ("schema_invalid", "allowlist_violation", "policy_bounds_exceeded", "loop_detected"):
        match = GuardMatch(rule=rule, confidence=1.0, detail={})
        for profile in PROFILES.values():
            assert decide(match, profile) == "BLOCK"


def test_escalation_signal_always_escalates() -> None:
    match = GuardMatch(rule="escalation_threshold_reached", confidence=1.0, detail={})
    for profile in PROFILES.values():
        assert decide(match, profile) == "ESCALATE"


def test_pii_always_redacts_regardless_of_profile() -> None:
    match = GuardMatch(rule="pii_email", confidence=1.0, detail={})
    for profile in PROFILES.values():
        assert decide(match, profile) == "REDACT"


def test_strict_profile_always_escalates_on_any_injection_confidence() -> None:
    low_confidence = GuardMatch(rule="injection_roleplay_act_as", confidence=0.3, detail={})
    assert decide(low_confidence, PROFILES["strict"]) == "ESCALATE"


def test_standard_profile_blocks_high_confidence_injection() -> None:
    high_confidence = GuardMatch(rule="injection_system_override", confidence=0.9, detail={})
    assert decide(high_confidence, PROFILES["standard"]) == "BLOCK"


def test_standard_profile_redacts_low_confidence_injection() -> None:
    low_confidence = GuardMatch(rule="injection_roleplay_act_as", confidence=0.3, detail={})
    assert decide(low_confidence, PROFILES["standard"]) == "REDACT"


def test_lenient_profile_allows_low_confidence_injection() -> None:
    low_confidence = GuardMatch(rule="injection_roleplay_act_as", confidence=0.3, detail={})
    assert decide(low_confidence, PROFILES["lenient"]) == "ALLOW"


def test_lenient_profile_still_redacts_very_high_confidence_injection() -> None:
    high_confidence = GuardMatch(rule="injection_system_override", confidence=0.9, detail={})
    assert decide(high_confidence, PROFILES["lenient"]) == "REDACT"


def test_profile_caps_get_stricter_from_lenient_to_strict() -> None:
    assert (
        PROFILES["strict"].policy_caps["issue_refund"]["amount_inr"]
        < PROFILES["standard"].policy_caps["issue_refund"]["amount_inr"]
        < PROFILES["lenient"].policy_caps["issue_refund"]["amount_inr"]
    )
