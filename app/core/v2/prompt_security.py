from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FilterOutcome:
    text: str
    redacted: bool


class PromptInjectionFilter:
    """Treat all page text as untrusted and redact instruction-like content."""

    INJECTION_PATTERNS = (
        r"ignore\s+previous\s+instructions",
        r"disregard\s+above",
        r"system\s+prompt",
        r"developer\s+message",
        r"tool\s+call",
        r"exfiltrate",
        r"reveal\s+secrets",
        r"bypass\s+security",
        r"do\s+not\s+follow\s+policy",
    )

    def __init__(self) -> None:
        self._regexes = [re.compile(pattern, flags=re.IGNORECASE) for pattern in self.INJECTION_PATTERNS]

    def sanitize(self, text: str, max_len: int) -> FilterOutcome:
        if not text:
            return FilterOutcome(text="", redacted=False)

        normalized = " ".join(text.split())
        redacted = False

        for regex in self._regexes:
            if regex.search(normalized):
                normalized = regex.sub("[filtered_instruction]", normalized)
                redacted = True

        return FilterOutcome(text=normalized[:max_len], redacted=redacted)
