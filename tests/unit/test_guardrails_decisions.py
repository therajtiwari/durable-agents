from durable_agents.guardrails.decisions import PROFILES, decide, get_profile
from durable_agents.guardrails.types import GuardMatch


def test_unknown_profile_falls_back_to_standard() -> None:
    assert get_profile(None) is PROFILES["standard"]
    assert get_profile("something-nobody-configured") is PROFILES["standard"]


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
