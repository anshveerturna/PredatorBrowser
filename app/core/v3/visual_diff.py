"""VisualDiffVerifier — Pillow-based pre/post screenshot comparison.

Computes structural similarity between two screenshots to detect
whether the UI actually changed after an action.  No VLM call,
no network call — runs in < 50 ms locally.

Usage:
    verifier = VisualDiffVerifier()
    result = await verifier.compare(pre_png, post_png, threshold=0.95)
    if not result.changed:
        # The click likely missed — trigger retry or escalation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisualDiffResult:
    """Result of a visual comparison between two screenshots."""

    changed: bool  # True if the page visually changed
    similarity: float  # 0.0 (totally different) to 1.0 (identical)
    diff_region: tuple[int, int, int, int] | None  # (x, y, w, h) bbox of largest change area
    pixel_diff_ratio: float  # fraction of pixels that changed


class VisualDiffVerifier:
    """Lightweight pre/post screenshot comparison.

    Parameters
    ----------
    default_threshold : float
        Similarity above this value is considered "no change" (action
        had no visible effect).  Default: 0.95.
    resize_to : tuple[int, int] | None
        Resize both images to this (width, height) before comparison
        for consistent performance.  Default: (384, 256).
    change_threshold_px : int
        Minimum per-pixel intensity difference to count as "changed".
        Default: 30 (out of 255).
    """

    def __init__(
        self,
        default_threshold: float = 0.95,
        resize_to: tuple[int, int] | None = (384, 256),
        change_threshold_px: int = 30,
    ) -> None:
        self._default_threshold = default_threshold
        self._resize_to = resize_to
        self._change_threshold_px = change_threshold_px

    async def compare(
        self,
        pre_screenshot: bytes,
        post_screenshot: bytes,
        threshold: float | None = None,
        mask_regions: tuple[tuple[float, float, float, float], ...] = (),
        roi: tuple[float, float, float, float] | None = None,
    ) -> VisualDiffResult:
        """Compare two screenshots and return a diff result.

        Parameters
        ----------
        pre_screenshot : bytes
            PNG bytes of the page before the action.
        post_screenshot : bytes
            PNG bytes of the page after the action.
        threshold : float | None
            Override the default similarity threshold.
        mask_regions : tuple[tuple[float, float, float, float], ...]
            Normalised bboxes (x, y, w, h) to ignore (e.g. cookie banners, animations).
            Blacked out in both images before diffing.
        roi : tuple[float, float, float, float] | None
            Normalised bbox (x, y, w, h) region of interest. Images are cropped 
            to this region before diffing.

        Returns
        -------
        VisualDiffResult
            Whether the page changed, similarity score, and diff region.
        """
        effective_threshold = threshold if threshold is not None else self._default_threshold

        try:
            from PIL import Image
        except ImportError:
            logger.warning(
                "Pillow not installed — assuming page changed (safe fallback)"
            )
            return VisualDiffResult(
                changed=True,
                similarity=0.0,
                diff_region=None,
                pixel_diff_ratio=1.0,
            )

        pre_img = Image.open(BytesIO(pre_screenshot)).convert("L")  # grayscale
        post_img = Image.open(BytesIO(post_screenshot)).convert("L")

        width, height = pre_img.size

        # 1. Apply ROI crop
        if roi:
            rx, ry, rw, rh = roi
            crop_box = (
                int(rx * width),
                int(ry * height),
                int((rx + rw) * width),
                int((ry + rh) * height),
            )
            pre_img = pre_img.crop(crop_box)
            post_img = post_img.crop(crop_box)
        else:
            crop_box = (0, 0, width, height)

        # 2. Apply masks (black out regions)
        if mask_regions:
            from PIL import ImageDraw
            pre_draw = ImageDraw.Draw(pre_img)
            post_draw = ImageDraw.Draw(post_img)
            for mx, my, mw, mh in mask_regions:
                # Calculate mask box relative to the original image
                abs_x = int(mx * width) - crop_box[0]
                abs_y = int(my * height) - crop_box[1]
                abs_w = int(mw * width)
                abs_h = int(mh * height)
                mask_box = [abs_x, abs_y, abs_x + abs_w, abs_y + abs_h]
                pre_draw.rectangle(mask_box, fill=0)
                post_draw.rectangle(mask_box, fill=0)

        if self._resize_to:
            pre_img = pre_img.resize(self._resize_to, Image.LANCZOS)
            post_img = post_img.resize(self._resize_to, Image.LANCZOS)

        # Ensure both images have the same dimensions.
        if pre_img.size != post_img.size:
            post_img = post_img.resize(pre_img.size, Image.LANCZOS)

        # Per-pixel absolute difference.
        width, height = pre_img.size
        pre_pixels = list(pre_img.getdata())
        post_pixels = list(post_img.getdata())

        changed_count = 0
        total_diff = 0
        # Track bounding box of changed region.
        min_x, min_y = width, height
        max_x, max_y = 0, 0

        for i, (p, q) in enumerate(zip(pre_pixels, post_pixels)):
            diff = abs(p - q)
            total_diff += diff
            if diff > self._change_threshold_px:
                changed_count += 1
                x = i % width
                y = i // width
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)

        total_pixels = len(pre_pixels)
        pixel_diff_ratio = changed_count / total_pixels if total_pixels > 0 else 0.0

        # Compute normalised similarity (1.0 = identical).
        max_possible_diff = 255.0 * total_pixels
        similarity = 1.0 - (total_diff / max_possible_diff) if max_possible_diff > 0 else 1.0

        # Diff region (only valid if there were changes).
        diff_region: tuple[int, int, int, int] | None = None
        if changed_count > 0 and max_x >= min_x and max_y >= min_y:
            diff_region = (min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)

        changed = similarity < effective_threshold

        logger.debug(
            "VisualDiff: similarity=%.4f threshold=%.4f changed=%s diff_ratio=%.4f",
            similarity,
            effective_threshold,
            changed,
            pixel_diff_ratio,
        )

        return VisualDiffResult(
            changed=changed,
            similarity=similarity,
            diff_region=diff_region,
            pixel_diff_ratio=pixel_diff_ratio,
        )
