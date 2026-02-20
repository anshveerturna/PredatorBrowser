"""NavigatorV3 — Wraps v2 Navigator with intent-ranked binding.

Extends the v2 ``Navigator`` without modifying it.  When an
``ActionSpec`` lacks an explicit selector or eid that resolves
on the first try, NavigatorV3 falls through to the ``IntentRanker``
to pick the best-scoring candidate.

Integration:
    Inject ``NavigatorV3`` in place of ``Navigator`` when constructing
    ``ActionEngine``.  All existing contracts continue to work because
    NavigatorV3 preserves the parent's interface and only adds ranked
    fallback behaviour.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from playwright.async_api import Page

from app.core.v2.contracts import ActionSpec, ActionType
from app.core.v2.navigator import BoundTarget, Navigator
from app.core.v2.state_models import StructuredState
from app.core.v3.intent_ranker import IntentRanker, RankedCandidate

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class NavigatorV3(Navigator):
    """Navigator with intent-aware ranked element binding.

    Behaviour:
      1. If the ``ActionSpec`` has an explicit ``selector`` → v2 path (unchanged).
      2. If the ``ActionSpec`` has a ``target_eid`` that resolves → v2 path.
      3. Otherwise → rank all state elements via ``IntentRanker``, try
         the top-K candidates until one binds successfully.
    """

    def __init__(
        self,
        page: Page,
        ranker: IntentRanker | None = None,
        max_ranked_candidates: int = 5,
        ambiguity_threshold: float = 0.10,
    ) -> None:
        super().__init__(page)
        self._ranker = ranker or IntentRanker()
        self._max_candidates = max_ranked_candidates
        self._ambiguity_threshold = ambiguity_threshold
        self._last_ranking: tuple[RankedCandidate, ...] = ()

    @property
    def last_ranking(self) -> tuple[RankedCandidate, ...]:
        """Most recent ranking result — useful for debugging and audit."""
        return self._last_ranking

    def bind_target(
        self,
        action_spec: ActionSpec,
        state: StructuredState,
        *,
        intent: str = "",
        action_type: ActionType | None = None,
    ) -> BoundTarget:
        """Bind a target element using v2 logic, falling through to ranked
        binding when the deterministic path cannot resolve.

        Parameters
        ----------
        action_spec : ActionSpec
            The action specification from the contract.
        state : StructuredState
            Current structured page state.
        intent : str
            Natural-language intent from the ``ActionContract.intent``
            field.  Used only when the ranked fallback activates.
        action_type : ActionType | None
            The action type from the spec, used for role matching.
        """
        # ── Path 1: explicit selector (highest confidence, v2-unchanged) ──
        if action_spec.selector:
            logger.debug("NavigatorV3: explicit selector → v2 path")
            return super().bind_target(action_spec, state)

        # ── Path 2: explicit eid (v2-unchanged) ──
        if action_spec.target_eid:
            try:
                bound = super().bind_target(action_spec, state)
                logger.debug(
                    "NavigatorV3: eid %s resolved → v2 path (confidence=%s)",
                    action_spec.target_eid,
                    bound.confidence,
                )
                return bound
            except ValueError:
                logger.info(
                    "NavigatorV3: eid %s failed to bind, falling through to ranked path",
                    action_spec.target_eid,
                )

        # ── Path 3: selector_candidates (v2 tries first one blindly) ──
        if action_spec.selector_candidates and not intent:
            # No intent provided — fall back to v2 behaviour.
            logger.debug("NavigatorV3: selector_candidates without intent → v2 path")
            return super().bind_target(action_spec, state)

        # ── Path 4: Ranked binding ──────────────────────────────────
        effective_action_type = action_type or action_spec.action_type
        self._last_ranking = self._ranker.rank(
            state.interactive_elements,
            intent,
            effective_action_type,
        )

        if not self._last_ranking:
            raise ValueError("NavigatorV3: no elements to rank")

        top = self._last_ranking[: self._max_candidates]
        logger.info(
            "NavigatorV3: ranked %d elements, trying top %d (best score=%.3f, eid=%s)",
            len(self._last_ranking),
            len(top),
            top[0].score,
            top[0].eid,
        )

        # Ambiguity check: prevent blind clicking if top 2 candidates are too close.
        if len(top) > 1 and (top[0].score - top[1].score) <= self._ambiguity_threshold:
            logger.warning(
                "NavigatorV3: Ambiguous top candidates (score diff %.3f <= %.3f). eids: %s, %s",
                top[0].score - top[1].score,
                self._ambiguity_threshold,
                top[0].eid,
                top[1].eid,
            )
            raise ValueError(
                f"NavigatorV3: Ambiguous ranking for intent '{intent}' "
                f"(top scores {top[0].score:.3f} and {top[1].score:.3f})"
            )

        # Try each ranked candidate until one binds.
        for candidate in top:
            selector, fid = self._selector_from_eid(state, candidate.eid)
            if selector:
                logger.info(
                    "NavigatorV3: bound ranked candidate eid=%s score=%.3f signals=%s",
                    candidate.eid,
                    candidate.score,
                    candidate.match_signals,
                )
                return BoundTarget(
                    eid=candidate.eid,
                    fid=fid,
                    selector=selector,
                    confidence=candidate.score,
                )

        raise ValueError(
            f"NavigatorV3: none of top-{len(top)} ranked candidates could be bound"
        )
