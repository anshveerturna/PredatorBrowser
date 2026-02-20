"""VisionGate — VLM-based disambiguation on escalation only.

Activated exclusively by ``EscalationMode.VISION_FALLBACK`` when the
structured binding path (including IntentRanker) has failed after all
retry attempts.

Flow:
    1. Screenshot current viewport
    2. Draw numbered labels over the top-K candidate bounding boxes
       (Set-of-Mark overlay)
    3. Build a compact text prompt
    4. Send annotated image + prompt to configured VLM endpoint
    5. Parse the VLM response → return a ``BoundTarget`` or ``None``

Token budget: ~300 tokens per escalation (single VLM call).

The annotated screenshot is persisted as an artifact for replay/audit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Callable, Coroutine

from playwright.async_api import Page

from app.core.v2.navigator import BoundTarget
from app.core.v2.state_models import InteractiveElementState, StructuredState
from app.core.v3.intent_ranker import RankedCandidate

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class VisionGateConfig:
    """Configuration for the VisionGate module."""

    # Maximum candidates to overlay on the screenshot.
    max_overlay_candidates: int = 5

    # Image dimensions for the annotated screenshot (resized before encoding).
    image_width: int = 768
    image_height: int = 512

    # Whether to persist the annotated screenshot as an artifact.
    persist_artifacts: bool = True


# ──────────────────────────────────────────────
# Set-of-Mark overlay
# ──────────────────────────────────────────────

def _draw_som_overlay(
    screenshot_png: bytes,
    candidates: list[tuple[int, InteractiveElementState]],
    viewport_width: int,
    viewport_height: int,
    target_width: int = 768,
    target_height: int = 512,
) -> bytes:
    """Draw numbered bounding boxes (Set-of-Mark) on a screenshot.

    Each candidate gets a coloured rectangle with a number label
    at the top-left corner.  Returns the annotated image as PNG bytes.

    Uses Pillow — a lightweight dependency with no VLM involvement.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("Pillow not installed — returning raw screenshot without overlay")
        return screenshot_png

    img = Image.open(BytesIO(screenshot_png)).convert("RGB")
    img = img.resize((target_width, target_height), Image.LANCZOS)
    draw = ImageDraw.Draw(img)

    # Scale factors from viewport to resized image.
    sx = target_width / viewport_width
    sy = target_height / viewport_height

    # Colour palette for overlays.
    colours = [
        (255, 50, 50),    # red
        (50, 200, 50),    # green
        (50, 100, 255),   # blue
        (255, 165, 0),    # orange
        (200, 50, 200),   # purple
    ]

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
        except Exception:
            font = ImageFont.load_default()

    for idx, (label_num, element) in enumerate(candidates):
        if not element.bbox_norm or len(element.bbox_norm) < 4:
            continue

        bx, by, bw, bh = element.bbox_norm
        # Convert normalised bbox to pixel coords in the resized image.
        px = int(bx * viewport_width * sx)
        py = int(by * viewport_height * sy)
        pw = int(bw * viewport_width * sx)
        ph = int(bh * viewport_height * sy)

        colour = colours[idx % len(colours)]
        draw.rectangle([px, py, px + pw, py + ph], outline=colour, width=2)

        # Draw label background + number.
        label = str(label_num)
        text_bbox = draw.textbbox((0, 0), label, font=font)
        text_w = text_bbox[2] - text_bbox[0] + 6
        text_h = text_bbox[3] - text_bbox[1] + 4
        draw.rectangle([px, py - text_h, px + text_w, py], fill=colour)
        draw.text((px + 3, py - text_h + 1), label, fill="white", font=font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ──────────────────────────────────────────────
# Prompt builder
# ──────────────────────────────────────────────

def _build_vision_prompt(
    intent: str,
    candidates: list[tuple[int, InteractiveElementState]],
) -> str:
    """Build a compact text prompt for the VLM.

    Keeps the text under 100 tokens.
    """
    lines = [
        f"Task: {intent}",
        "The screenshot shows numbered elements. Which number should be clicked?",
        "Elements:",
    ]
    for label_num, element in candidates:
        role = element.role or "unknown"
        name = (element.name_short or "")[:60]
        lines.append(f"  [{label_num}] {role}: {name}")

    lines.append("Reply with ONLY the number of the best element, or 0 if none fit.")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# VisionGate
# ──────────────────────────────────────────────

# Type alias for the VLM call function that users must inject.
# Signature: (image_base64: str, prompt: str) -> str
VLMCallable = Callable[[str, str], Coroutine[Any, Any, str]]

@dataclass(frozen=True)
class VisionGateResult:
    """Rich result containing everything needed for deterministic audit replay."""
    target: BoundTarget | None
    prompt: str
    response: str
    image_hash: str
    candidates_overlayed: tuple[tuple[int, str], ...]  # (label_num, eid)


class VisionGate:
    """VLM-based disambiguation — triggered only on VISION_FALLBACK escalation.

    Parameters
    ----------
    vlm_call : VLMCallable
        An async function that accepts ``(image_base64, prompt)`` and
        returns the VLM's text response.  This keeps VisionGate
        provider-agnostic (works with Gemini, GPT-4o, local models).
    config : VisionGateConfig | None
        Optional configuration overrides.
    """

    def __init__(
        self,
        vlm_call: VLMCallable,
        config: VisionGateConfig | None = None,
    ) -> None:
        self._vlm_call = vlm_call
        self._config = config or VisionGateConfig()
        self._last_annotated_image: bytes | None = None
        self._last_prompt: str | None = None
        self._last_response: str | None = None

    @property
    def last_annotated_image(self) -> bytes | None:
        """Last annotated screenshot — for artifact persistence."""
        return self._last_annotated_image

    @property
    def last_prompt(self) -> str | None:
        return self._last_prompt

    @property
    def last_response(self) -> str | None:
        return self._last_response

    async def resolve(
        self,
        page: Page,
        ranked_candidates: tuple[RankedCandidate, ...],
        state: StructuredState,
        intent: str,
        viewport_width: int = 1440,
        viewport_height: int = 900,
    ) -> BoundTarget | None:
        """Attempt to resolve a target element using VLM vision.

        Parameters
        ----------
        page : Page
            The Playwright page to screenshot.
        ranked_candidates : tuple[RankedCandidate, ...]
            Top candidates from the IntentRanker.
        state : StructuredState
            Current structured page state (for eid → element lookup).
        intent : str
            The natural-language intent from the contract.
        viewport_width, viewport_height : int
            Current viewport size for bbox denormalisation.

        Returns
        -------
        VisionGateResult
            The resolution attempt, including prompt and VLM response.
        """
        # Build list of (label_number, element) for the top-K candidates.
        eid_to_element = {e.eid: e for e in state.interactive_elements}
        labelled: list[tuple[int, InteractiveElementState]] = []
        for i, candidate in enumerate(ranked_candidates[: self._config.max_overlay_candidates]):
            element = eid_to_element.get(candidate.eid)
            if element:
                labelled.append((i + 1, element))

        if not labelled:
            logger.warning("VisionGate: no valid candidates to overlay")
            return None

        # 1. Screenshot
        screenshot_png = await page.screenshot(type="png")

        # 2. Annotate
        annotated = _draw_som_overlay(
            screenshot_png,
            labelled,
            viewport_width,
            viewport_height,
            self._config.image_width,
            self._config.image_height,
        )
        self._last_annotated_image = annotated

        # 3. Build prompt
        prompt = _build_vision_prompt(intent, labelled)
        self._last_prompt = prompt

        image_b64 = base64.b64encode(annotated).decode("ascii")
        image_hash = hashlib.sha256(annotated).hexdigest()
        
        try:
            response = await self._vlm_call(image_b64, prompt)
        except Exception as exc:
            logger.error("VisionGate: VLM call failed: %s", exc)
            return VisionGateResult(
                target=None,
                prompt=prompt,
                response=f"ERROR: {exc}",
                image_hash=image_hash,
                candidates_overlayed=tuple((n, e.eid) for n, e in labelled),
            )
        self._last_response = response

        # 5. Parse response — expect a single number.
        chosen = self._parse_response(response, labelled)
        
        if chosen is None:
            logger.info("VisionGate: VLM response inconclusive: %r", response)
            return VisionGateResult(
                target=None,
                prompt=prompt,
                response=response,
                image_hash=image_hash,
                candidates_overlayed=tuple((n, e.eid) for n, e in labelled),
            )

        label_num, element = chosen
        # Resolve to selector via the element's selector_hints.
        selector = None
        if element.selector_hints:
            selector = element.selector_hints[0]
        elif element.role and element.name_short:
            selector = f'role={element.role}[name="{element.name_short}"]'
        elif element.name_short:
            selector = f'text="{element.name_short}"'

        if not selector:
            logger.warning("VisionGate: chosen element has no usable selector")
            return VisionGateResult(
                target=None,
                prompt=prompt,
                response=response,
                image_hash=image_hash,
                candidates_overlayed=tuple((n, e.eid) for n, e in labelled),
            )

        logger.info(
            "VisionGate: VLM chose label=%d eid=%s selector=%s",
            label_num,
            element.eid,
            selector,
        )
        target = BoundTarget(
            eid=element.eid,
            fid=element.fid,
            selector=selector,
            confidence=0.6,  # moderate confidence — VLM-assisted
        )
        
        return VisionGateResult(
            target=target,
            prompt=prompt,
            response=response,
            image_hash=image_hash,
            candidates_overlayed=tuple((n, e.eid) for n, e in labelled),
        )

    @staticmethod
    def _parse_response(
        response: str,
        labelled: list[tuple[int, InteractiveElementState]],
    ) -> tuple[int, InteractiveElementState] | None:
        """Extract the chosen label number from the VLM response."""
        # Strip whitespace, try to find a single digit.
        response = response.strip()
        label_map = {label_num: element for label_num, element in labelled}

        # Try exact match first.
        try:
            num = int(response)
            if num == 0:
                return None  # VLM said "none fit"
            if num in label_map:
                return num, label_map[num]
        except ValueError:
            pass

        # Try to find any mentioned number in the response text.
        import re
        numbers = re.findall(r"\b(\d+)\b", response)
        for n_str in numbers:
            n = int(n_str)
            if n in label_map:
                return n, label_map[n]

        return None
