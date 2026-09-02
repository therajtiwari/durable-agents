from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardMatch:
    """One thing a scanner found. Detection only — no action attached.

    decisions.py maps this to a GuardAction; the scanner itself has no
    opinion on what should happen next.
    """

    rule: str
    confidence: float
    detail: dict[str, Any]


@dataclass(frozen=True)
class ScanResult:
    matches: list[GuardMatch]
    redacted_text: str
    """Equal to the original text when no PII pattern matched. Redaction
    is mechanical (replace a matched span with a placeholder) so it's
    computed unconditionally here; whether it's actually used instead of
    the original is decisions.py's call, not the scanner's.
    """
