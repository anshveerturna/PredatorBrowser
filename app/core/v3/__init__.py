"""Predator v3 — Hybrid UI Intelligence Layer.

Additive modules that enhance v2's element selection and disambiguation
without modifying the core deterministic execution engine.

Modules:
    - IntentRanker: Pure-function element scoring by intent relevance
    - NavigatorV3: Wraps v2 Navigator with ranked binding
    - VisionGate: VLM-based disambiguation (escalation only)
    - VisualDiffVerifier: Screenshot-based post-action verification
"""

from app.core.v3.intent_ranker import IntentRanker, RankedCandidate
from app.core.v3.navigator_v3 import NavigatorV3
from app.core.v3.vision_gate import VisionGate, VisionGateConfig
from app.core.v3.visual_diff import VisualDiffResult, VisualDiffVerifier

__all__ = [
    "IntentRanker",
    "NavigatorV3",
    "RankedCandidate",
    "VisionGate",
    "VisionGateConfig",
    "VisualDiffResult",
    "VisualDiffVerifier",
]
