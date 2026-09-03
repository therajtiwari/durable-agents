import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from durable_agents.guardrails.types import GuardMatch

# Deliberately not exhaustive — a starting corpus, meant to be tuned
# against the attack corpus once one exists (see docs/THREAT_MODEL.md's
# evaluation plan). Confidence is a rough prior, not a measured number
# yet: phrases that are almost never legitimate get a high score,
# phrases with plausible benign uses ("act as a project manager" is a
# completely normal instruction) get a lower one so decisions.py can
# choose to log-only rather than block on them.
INJECTION_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("ignore_instructions", re.compile(r"ignore (all |any )?(previous|prior|above) instructions", re.I), 0.9),
    ("disregard_prior", re.compile(r"disregard (the )?(above|previous|prior)", re.I), 0.85),
    ("forget_prior", re.compile(r"forget (your |our )?(previous|prior) (rules|instructions)", re.I), 0.85),
    ("new_instructions", re.compile(r"\bnew instructions\b", re.I), 0.7),
    ("system_override", re.compile(r"\bSYSTEM\s*:|\bSYSTEM\s+OVERRIDE\b", re.I), 0.9),
    ("new_persona", re.compile(r"\byou are now\b|\bpretend (that )?you are\b", re.I), 0.6),
    ("roleplay_act_as", re.compile(r"\bact as\b|\broleplay as\b", re.I), 0.3),
    ("hypothetical_no_limits", re.compile(r"hypothetically,? if you (had|have) no\b", re.I), 0.6),
    ("lets_play_pretend", re.compile(r"let'?s (play a game|pretend)\b", re.I), 0.6),
    ("not_bound_by_rules", re.compile(r"not bound by (any )?rules\b", re.I), 0.7),
    ("jailbreak_mode", re.compile(r"\bdeveloper mode\b|\bDAN mode\b|\bno restrictions\b", re.I), 0.85),
    ("base64_blob", re.compile(r"(?:[A-Za-z0-9+/]{4}){10,}={0,2}"), 0.5),
]


def scan_patterns(text: str) -> list[GuardMatch]:
    """Injection matches DO record the text they matched, deliberately.

    That text is the attack, not the victim — it is what makes "has this
    agent ever been targeted, and how?" answerable from the event log,
    which is most of the argument for guardrail decisions living there at
    all. Contrast scan_pii below, where the matched text is precisely the
    thing that must never be written down.
    """

    matches: list[GuardMatch] = []
    for rule, regex, confidence in INJECTION_PATTERNS:
        m = regex.search(text)
        if m is not None:
            matches.append(
                GuardMatch(rule=f"injection_{rule}", confidence=confidence, detail={"matched": m.group()})
            )
    return matches


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _credit_card_valid(raw: str) -> bool:
    digits = re.sub(r"[ -]", "", raw)
    return 13 <= len(digits) <= 19 and _luhn_valid(digits)


def _phone_valid(raw: str) -> bool:
    # 10-15 digits covers real international numbers without assuming a
    # country's length. This backs up the regex rather than carrying it:
    # see PHONE_PATTERN below for why the shape has to do most of the
    # work.
    digits = re.sub(r"\D", "", raw)
    return 10 <= len(digits) <= 15


# Phone numbers have no checksum and no unambiguous shape, unlike the
# other defaults (email, Luhn-checked card, IBAN). An earlier version
# matched any run of 10-15 digits, which meant it quietly ate ordinary
# business identifiers — order numbers, invoice numbers, tracking codes —
# and replaced them with <PHONE_1> in what the model was sent. That
# failure is silent and expensive: the agent asks about order
# 1234567890123, the model sees a placeholder, and nothing anywhere
# reports an error.
#
# So the shape must look like a dialable number rather than merely a
# long number: an international "+" prefix, or digits genuinely grouped
# by separators. The lookarounds keep it from starting or ending inside
# a longer identifier (INV-99887766554, 1Z999AA10123456784) or from
# matching a window inside a run of unrelated numbers ("8891 6400 3000
# 1200"). Verified against both a set of real formats and a set of
# business identifiers in tests/unit/test_guardrails_input_scan.py.
PHONE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?<!\d[\s\-])"
    r"(?:\+\d[\d\s\-()]{7,16}\d|\(?\d{2,4}\)?[\s\-]\d{2,4}[\s\-]\d{2,6})"
    r"(?![\s\-]?\d)(?![A-Za-z0-9])"
)


@dataclass(frozen=True)
class PIIPattern:
    name: str
    regex: re.Pattern[str]
    validate: Callable[[str], bool] | None = None
    """Extra check beyond the regex, applied to the raw matched text —
    e.g. the credit card pattern is deliberately loose (any run of
    13-19 digits) and relies on this to reject the many non-card
    numbers that shape alone would also match.
    """


# Locale-neutral by design — see docs/THREAT_MODEL.md's "PII patterns
# are pluggable" section. No country-specific ID formats belong here;
# a consumer (or this project's own refund demo) adds those by passing
# its own PIIPattern list, not by extending this default set.
DEFAULT_PII_PATTERNS: list[PIIPattern] = [
    # The leading lookbehind is a performance fix, not a semantic one: it
    # matches exactly what the unanchored version did (verified against a
    # correctness set), but stops the engine restarting the "+" at every
    # position inside a long run of identifier characters and backtracking
    # over it. Without it this was quadratic — a tool returning 80 KB of
    # log or base64 took 7.5 seconds of pure CPU, synchronously, inside
    # the event loop, stalling every other run the worker was handling.
    # With it, the same input takes ~2 ms.
    PIIPattern(
        "email",
        re.compile(r"(?<![a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    ),
    PIIPattern("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b"), validate=_credit_card_valid),
    PIIPattern("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")),
    PIIPattern("phone", PHONE_PATTERN, validate=_phone_valid),
]


def scan_pii(
    text: str, patterns: Sequence[PIIPattern] = DEFAULT_PII_PATTERNS
) -> tuple[list[GuardMatch], str]:
    """Returns (matches, redacted_text). Redaction is mechanical — the
    action of actually using redacted_text instead of the original is
    decisions.py's call, not this function's.

    A match records what KIND of thing was found and where, never the
    value. Every GuardMatch here ends up on a GuardrailTriggered event,
    which is appended to a log that by design is never updated and never
    deleted — so a card number written into it could not be removed
    afterwards even in principle, and the event whose entire purpose is
    recording that a secret was redacted would be the thing storing it.
    SPEC.md section 15 specifies this payload shape and calls getting it
    wrong "the difference between an audit log and a data breach".

    The span is offsets into the text that was scanned, which is enough
    to line a hit up against the redacted content an auditor can see,
    without reproducing the original.

    On overlapping matches from different patterns (e.g. the loose
    phone pattern catching part of a credit card number), the earlier
    entry in `patterns` wins and the overlapping candidate is dropped —
    deterministic, but does mean pattern order in the list you pass
    matters.
    """

    candidates: list[tuple[PIIPattern, re.Match[str]]] = []
    for pattern in patterns:
        for m in pattern.regex.finditer(text):
            if pattern.validate is None or pattern.validate(m.group()):
                candidates.append((pattern, m))
    candidates.sort(key=lambda pm: pm[1].start())

    accepted: list[tuple[PIIPattern, re.Match[str]]] = []
    last_end = -1
    for pattern, m in candidates:
        if m.start() < last_end:
            continue
        accepted.append((pattern, m))
        last_end = m.end()

    matches: list[GuardMatch] = []
    replacements: list[tuple[int, int, str]] = []
    counters: dict[str, int] = {}
    for pattern, m in accepted:
        counters[pattern.name] = counters.get(pattern.name, 0) + 1
        placeholder = f"<{pattern.name.upper()}_{counters[pattern.name]}>"
        matches.append(
            GuardMatch(
                rule=f"pii_{pattern.name}",
                confidence=1.0,
                detail={
                    "entity": pattern.name,
                    "placeholder": placeholder,
                    "span": [m.start(), m.end()],
                },
            )
        )
        replacements.append((m.start(), m.end(), placeholder))

    redacted = text
    for start, end, placeholder in sorted(replacements, key=lambda r: r[0], reverse=True):
        redacted = redacted[:start] + placeholder + redacted[end:]

    return matches, redacted
