"""IntentRanker — Pure-function element scoring by intent relevance.

Scores extracted InteractiveElementState items against a contract's
intent string using six weighted signals.  No randomness, no external
calls — same inputs always produce the same output (replay-safe).

Scoring signals (default weights):
    text_match   0.40  — normalised token overlap (intent vs name_short)
    role_match   0.20  — action_type → expected role mapping
    stability    0.15  — v2 stability_score (elements with #id / data-testid)
    spatial      0.10  — center-of-viewport priority
    enabled      0.10  — enabled=True ≫ disabled
    specificity  0.05  — more selector_hints → higher re-findability
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from app.core.v2.contracts import ActionType
from app.core.v2.state_models import InteractiveElementState


# ──────────────────────────────────────────────
# Public data model & Configuration
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class RankerConfig:
    version: str = "v3.0.0"
    weights: dict[str, float] = field(default_factory=lambda: {
        "text_match": 0.40,
        "role_match": 0.20,
        "stability": 0.15,
        "spatial": 0.10,
        "enabled": 0.10,
        "specificity": 0.05,
    })


@dataclass(frozen=True)
class RankedCandidate:
    """An element scored for relevance to a given intent."""

    eid: str
    score: float  # 0.0–1.0
    match_signals: tuple[str, ...]  # e.g. ("text_match:0.92", "role_boost")
    ranker_version: str  # Added for audit/replay safety


# ──────────────────────────────────────────────
# Scoring constants
# ──────────────────────────────────────────────

# Maps ActionType → set of roles that are most expected for that action.
_ACTION_ROLE_MAP: dict[ActionType, frozenset[str]] = {
    ActionType.CLICK: frozenset({"button", "a", "link", "tab", "menuitem", "switch"}),
    ActionType.TYPE: frozenset({"input", "textarea", "textbox", "searchbox", "combobox"}),
    ActionType.SELECT: frozenset({"select", "combobox", "listbox"}),
    ActionType.NAVIGATE: frozenset({"a", "link"}),
    ActionType.UPLOAD: frozenset({"input"}),
    ActionType.DOWNLOAD_TRIGGER: frozenset({"button", "a", "link"}),
}

# Pre-compiled regex for tokenisation.
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


# ──────────────────────────────────────────────
# Helper: token-level similarity
# ──────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """Lowercased alphanumeric tokens."""
    return set(tok.lower() for tok in _TOKEN_RE.findall(text) if len(tok) > 1)


def _token_overlap(intent: str, name: str) -> float:
    """Normalised Jaccard-like overlap between intent tokens and name tokens."""
    intent_tokens = _tokenize(intent)
    name_tokens = _tokenize(name)
    if not intent_tokens:
        return 0.0
    intersection = intent_tokens & name_tokens
    if not intersection:
        return 0.0
    # Weighted toward recall: how many intent tokens appear in the name?
    recall = len(intersection) / len(intent_tokens)
    precision = len(intersection) / len(name_tokens) if name_tokens else 0.0
    # F1-like harmonic mean, but biased toward recall (intent coverage).
    if recall + precision == 0:
        return 0.0
    return (2.0 * recall * precision) / (recall + precision)


# ──────────────────────────────────────────────
# IntentRanker
# ──────────────────────────────────────────────

class IntentRanker:
    """Scores interactive elements against a contract intent.

    Parameters
    ----------
    config : RankerConfig | None
        Configuration containing scoring weights and version string.
        Defaults to the current RankerConfig.
    """

    def __init__(self, config: RankerConfig | None = None) -> None:
        self._config = config or RankerConfig()
        self._weights = dict(self._config.weights)
        
        # Normalise so weights always sum to 1.0.
        total = sum(self._weights.values())
        if total > 0:
            self._weights = {k: v / total for k, v in self._weights.items()}

    @property
    def version(self) -> str:
        return self._config.version

    # ── Individual signal scorers ──────────────

    @staticmethod
    def _score_text_match(intent: str, element: InteractiveElementState) -> float:
        """Token overlap between intent and element name."""
        name = element.name_short or ""
        return _token_overlap(intent, name)

    @staticmethod
    def _score_role_match(
        action_type: ActionType | None,
        element: InteractiveElementState,
    ) -> float:
        """1.0 if the element role matches the expected roles for this action type."""
        if action_type is None:
            return 0.5  # neutral when action type is unknown
        expected = _ACTION_ROLE_MAP.get(action_type)
        if not expected:
            return 0.5
        role = (element.role or "").lower()
        return 1.0 if role in expected else 0.2

    @staticmethod
    def _score_stability(element: InteractiveElementState) -> float:
        """Uses v2's existing stability_score (0.4–0.8 range, normalise to 0.0–1.0)."""
        return min(1.0, max(0.0, (element.stability_score - 0.3) / 0.5))

    @staticmethod
    def _score_spatial(element: InteractiveElementState) -> float:
        """Center-of-viewport priority.  Elements in the middle 60% of the
        viewport score 1.0; elements at the edges score lower."""
        if not element.bbox_norm or len(element.bbox_norm) < 4:
            return 0.5
        x, y, w, h = element.bbox_norm
        cx = x + w / 2.0
        cy = y + h / 2.0
        # Distance from center (0.5, 0.5), normalised to [0, 1].
        dist = ((cx - 0.5) ** 2 + (cy - 0.5) ** 2) ** 0.5
        max_dist = 0.707  # sqrt(0.5^2 + 0.5^2)
        return max(0.0, 1.0 - dist / max_dist)

    @staticmethod
    def _score_enabled(element: InteractiveElementState) -> float:
        """Enabled elements score 1.0, disabled score 0.1."""
        return 1.0 if element.enabled else 0.1

    @staticmethod
    def _score_specificity(element: InteractiveElementState) -> float:
        """More selector hints → higher confidence in re-findability."""
        hints = element.selector_hints or ()
        if len(hints) >= 3:
            return 1.0
        if len(hints) == 2:
            return 0.8
        if len(hints) == 1:
            return 0.5
        return 0.2

    # ── Composite scorer ──────────────────────

    def _score_element(
        self,
        element: InteractiveElementState,
        intent: str,
        action_type: ActionType | None,
    ) -> tuple[float, tuple[str, ...]]:
        """Compute weighted composite score and active signal labels."""
        signals: list[str] = []
        weighted_sum = 0.0

        text_score = self._score_text_match(intent, element)
        weighted_sum += text_score * self._weights["text_match"]
        if text_score > 0.3:
            signals.append(f"text_match:{text_score:.2f}")

        role_score = self._score_role_match(action_type, element)
        weighted_sum += role_score * self._weights["role_match"]
        if role_score >= 0.8:
            signals.append("role_boost")

        stability_score = self._score_stability(element)
        weighted_sum += stability_score * self._weights["stability"]
        if stability_score > 0.7:
            signals.append("stable_selector")

        spatial_score = self._score_spatial(element)
        weighted_sum += spatial_score * self._weights["spatial"]
        if spatial_score > 0.7:
            signals.append("spatial_center")

        enabled_score = self._score_enabled(element)
        weighted_sum += enabled_score * self._weights["enabled"]

        specificity_score = self._score_specificity(element)
        weighted_sum += specificity_score * self._weights["specificity"]
        if specificity_score >= 0.8:
            signals.append("high_specificity")

        return min(1.0, max(0.0, weighted_sum)), tuple(signals)

    # ── Public API ────────────────────────────

    def rank(
        self,
        elements: Sequence[InteractiveElementState],
        intent: str,
        action_type: ActionType | None = None,
    ) -> tuple[RankedCandidate, ...]:
        """Score and sort all elements by relevance to *intent*.

        Returns all elements sorted by descending score.  The consumer
        decides how many to use (top-K truncation).

        This is a **pure function** of ``(elements, intent, action_type)``
        — no randomness, no external API calls.  Replay-safe.
        """
        scored: list[tuple[float, RankedCandidate]] = []
        for element in elements:
            score, signals = self._score_element(element, intent, action_type)
            scored.append((
                score,
                RankedCandidate(
                    eid=element.eid, 
                    score=score, 
                    match_signals=signals,
                    ranker_version=self._config.version,
                ),
            ))

        # Sort descending by score, then by eid for deterministic tie-breaking.
        scored.sort(key=lambda pair: (-pair[0], pair[1].eid))
        return tuple(candidate for _, candidate in scored)
