"""
Level 3: The Vision Engine (Eagle Eye)
Set-of-Marks visual processing layer - High cost.

This module uses screenshots combined with Set-of-Marks (SoM) annotations
to enable visual understanding and interaction with elements that cannot
be accessed through the Accessibility Tree.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from playwright.async_api import Page, Locator
from openai import AsyncOpenAI

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    
logger = logging.getLogger("predator.vision")


@dataclass
class BoundingBox:
    """Represents an element's bounding box."""
    x: float
    y: float
    width: float
    height: float
    mark_id: int
    role: str = ""
    name: str = ""
    selector: str = ""
    
    @property
    def center(self) -> Tuple[float, float]:
        """Get center coordinates of the bounding box."""
        return (self.x + self.width / 2, self.y + self.height / 2)
    
    def contains_point(self, px: float, py: float) -> bool:
        """Check if a point is inside the bounding box."""
        return (self.x <= px <= self.x + self.width and
                self.y <= py <= self.y + self.height)


class VisionEngine:
    """
    Level 3: Eagle Eye - Vision-based Element Detection
    
    Uses screenshots with Set-of-Marks annotations to enable
    visual understanding when AX Tree navigation fails.
    """
    
    # Interactive selectors to look for
    INTERACTIVE_SELECTORS = [
        'button', 'a[href]', 'input', 'select', 'textarea',
        '[role="button"]', '[role="link"]', '[role="textbox"]',
        '[role="checkbox"]', '[role="radio"]', '[role="combobox"]',
        '[role="menuitem"]', '[role="tab"]', '[onclick]',
        '[tabindex]:not([tabindex="-1"])'
    ]
    
    # Colors for marks
    MARK_COLOR_BOX = (255, 0, 0)  # Red
    MARK_COLOR_TAG_BG = (255, 255, 255)  # White
    MARK_COLOR_TAG_TEXT = (255, 0, 0)  # Red
    
    def __init__(
        self,
        openai_client: AsyncOpenAI,
        vision_model: str = "gpt-4o"
    ) -> None:
        """
        Initialize the Vision Engine.
        
        Args:
            openai_client: AsyncOpenAI client for vision model
            vision_model: Model to use for visual analysis
        """
        self._openai = openai_client
        self._vision_model = vision_model
        self._page: Optional[Page] = None
        self._bounding_boxes: list[BoundingBox] = []
        
        if not PIL_AVAILABLE:
            logger.warning("[Vision] PIL/Pillow not available. Set-of-Marks will be limited.")
    
    async def attach(self, page: Page) -> None:
        """Attach vision engine to a page."""
        self._page = page
        self._bounding_boxes.clear()
        logger.info("[Vision] Attached to page")
    
    def detach(self) -> None:
        """Detach vision engine from page."""
        self._page = None
        self._bounding_boxes.clear()
        logger.info("[Vision] Detached from page")
    
    async def take_screenshot(self, full_page: bool = False) -> bytes:
        """
        Take a screenshot of the page.
        
        Args:
            full_page: Whether to capture full scrollable page
            
        Returns:
            PNG screenshot bytes
        """
        if not self._page:
            raise RuntimeError("Vision engine not attached to any page")
        
        return await self._page.screenshot(full_page=full_page, type="png")
    
    async def get_interactive_bounding_boxes(self) -> list[BoundingBox]:
        """
        Get bounding boxes for all interactive elements on the page.
        
        Returns:
            List of BoundingBox objects
        """
        if not self._page:
            return []
        
        self._bounding_boxes.clear()
        mark_id = 0
        
        # Combined selector for efficiency
        combined_selector = ", ".join(self.INTERACTIVE_SELECTORS)
        
        try:
            elements = await self._page.query_selector_all(combined_selector)
            
            for element in elements:
                try:
                    # Check if visible
                    is_visible = await element.is_visible()
                    if not is_visible:
                        continue
                    
                    # Get bounding box
                    box = await element.bounding_box()
                    if not box or box["width"] < 5 or box["height"] < 5:
                        continue
                    
                    # Get element info
                    role = await element.get_attribute("role") or ""
                    name = await element.inner_text() if await element.inner_text() else ""
                    name = name[:50] if name else ""  # Truncate long names
                    
                    # Build a selector for this element
                    tag_name = await element.evaluate("el => el.tagName.toLowerCase()")
                    element_id = await element.get_attribute("id")
                    
                    if element_id:
                        selector = f"#{element_id}"
                    else:
                        selector = f"{tag_name}"
                    
                    mark_id += 1
                    bbox = BoundingBox(
                        x=box["x"],
                        y=box["y"],
                        width=box["width"],
                        height=box["height"],
                        mark_id=mark_id,
                        role=role or tag_name,
                        name=name.strip().replace("\n", " "),
                        selector=selector
                    )
                    self._bounding_boxes.append(bbox)
                    
                except Exception as e:
                    logger.debug(f"[Vision] Error processing element: {e}")
                    continue
            
            logger.info(f"[Vision] Found {len(self._bounding_boxes)} interactive elements")
            return self._bounding_boxes
            
        except Exception as e:
            logger.error(f"[Vision] Error getting bounding boxes: {e}")
            return []
    
    async def apply_set_of_marks(self, screenshot_bytes: Optional[bytes] = None) -> bytes:
        """
        Apply Set-of-Marks annotations to a screenshot.
        
        Overlays red bounding boxes with white numbered tags on
        all interactive elements.
        
        Args:
            screenshot_bytes: Optional existing screenshot, or takes new one
            
        Returns:
            Annotated PNG image bytes
        """
        if not PIL_AVAILABLE:
            raise RuntimeError("PIL/Pillow required for Set-of-Marks")
        
        if not self._page:
            raise RuntimeError("Vision engine not attached to any page")
        
        # Take screenshot if not provided
        if screenshot_bytes is None:
            screenshot_bytes = await self.take_screenshot()
        
        # Get bounding boxes if not already collected
        if not self._bounding_boxes:
            await self.get_interactive_bounding_boxes()
        
        # Open image
        image = Image.open(io.BytesIO(screenshot_bytes))
        draw = ImageDraw.Draw(image)
        
        # Try to load a font, fall back to default
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except Exception:
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
            except Exception:
                font = ImageFont.load_default()
        
        # Draw marks for each bounding box
        for bbox in self._bounding_boxes:
            # Draw red bounding box
            draw.rectangle(
                [bbox.x, bbox.y, bbox.x + bbox.width, bbox.y + bbox.height],
                outline=self.MARK_COLOR_BOX,
                width=2
            )
            
            # Draw white tag with number
            tag_text = str(bbox.mark_id)
            
            # Calculate tag size
            try:
                text_bbox = draw.textbbox((0, 0), tag_text, font=font)
                text_width = text_bbox[2] - text_bbox[0]
                text_height = text_bbox[3] - text_bbox[1]
            except AttributeError:
                # Fallback for older Pillow versions
                text_width, text_height = draw.textsize(tag_text, font=font)
            
            tag_padding = 4
            tag_x = bbox.x
            tag_y = bbox.y - text_height - tag_padding * 2
            
            # Ensure tag is visible (move below if above viewport)
            if tag_y < 0:
                tag_y = bbox.y + bbox.height
            
            # Draw tag background
            draw.rectangle(
                [tag_x, tag_y, tag_x + text_width + tag_padding * 2, tag_y + text_height + tag_padding * 2],
                fill=self.MARK_COLOR_TAG_BG,
                outline=self.MARK_COLOR_BOX
            )
            
            # Draw tag number
            draw.text(
                (tag_x + tag_padding, tag_y + tag_padding),
                tag_text,
                fill=self.MARK_COLOR_TAG_TEXT,
                font=font
            )
        
        # Convert back to bytes
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    
    async def query_vision_model(
        self,
        goal: str,
        marked_image: Optional[bytes] = None
    ) -> Optional[dict[str, Any]]:
        """
        Query the vision model to identify the target element.
        
        Args:
            goal: User's objective (e.g., "Click the login button")
            marked_image: Pre-annotated image, or generates new one
            
        Returns:
            Dict with mark_id and coordinates, or None if not found
        """
        logger.info(f"[Vision] Querying vision model for: '{goal}'")
        
        # Get or create marked image
        if marked_image is None:
            marked_image = await self.apply_set_of_marks()
        
        # Encode image to base64
        image_base64 = base64.b64encode(marked_image).decode("utf-8")
        
        # Build element list for context
        elements_context = "\n".join([
            f"  {bbox.mark_id}: [{bbox.role}] \"{bbox.name}\"" if bbox.name else f"  {bbox.mark_id}: [{bbox.role}]"
            for bbox in self._bounding_boxes[:100]  # Limit for token efficiency
        ])
        
        prompt = f"""You are analyzing a screenshot with numbered red boxes marking interactive elements.

User Goal: {goal}

Elements Detected (number: [role] "name"):
{elements_context}

Instructions:
1. Look at the marked screenshot
2. Find the element that best matches the user's goal
3. Return ONLY the number of the target element
4. If no element matches, return -1

Respond with JSON only:
{{
    "mark_number": <number>,
    "confidence": 0.0-1.0,
    "reasoning": "brief explanation"
}}"""

        try:
            response = await self._openai.chat.completions.create(
                model=self._vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_base64}",
                                    "detail": "high"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=500,
                temperature=0
            )
            
            # Parse response
            content = response.choices[0].message.content or ""
            
            # Extract JSON from response (handle markdown code blocks)
            json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                logger.error(f"[Vision] Could not parse vision model response: {content}")
                return None
            
            mark_number = result.get("mark_number", -1)
            
            if mark_number == -1 or mark_number < 1:
                logger.info(f"[Vision] No matching element found. Reason: {result.get('reasoning', 'N/A')}")
                return None
            
            # Find the bounding box with this mark
            target_bbox = None
            for bbox in self._bounding_boxes:
                if bbox.mark_id == mark_number:
                    target_bbox = bbox
                    break
            
            if not target_bbox:
                logger.warning(f"[Vision] Mark number {mark_number} not found in bounding boxes")
                return None
            
            center_x, center_y = target_bbox.center
            
            logger.info(f"[Vision] ✓ Found element: Mark #{mark_number} at ({center_x:.0f}, {center_y:.0f})")
            
            return {
                "level": "L3_VISION",
                "mark_id": mark_number,
                "x": center_x,
                "y": center_y,
                "bounding_box": {
                    "x": target_bbox.x,
                    "y": target_bbox.y,
                    "width": target_bbox.width,
                    "height": target_bbox.height
                },
                "role": target_bbox.role,
                "name": target_bbox.name,
                "confidence": result.get("confidence", 0.5)
            }
            
        except Exception as e:
            logger.error(f"[Vision] Error querying vision model: {e}")
            return None
    
    async def click_by_coordinates(self, x: float, y: float) -> bool:
        """
        Click at specific coordinates on the page.
        
        Args:
            x: X coordinate
            y: Y coordinate
            
        Returns:
            True if click succeeded
        """
        if not self._page:
            return False
        
        try:
            await self._page.mouse.click(x, y)
            logger.info(f"[Vision] ✓ Clicked at coordinates ({x:.0f}, {y:.0f})")
            return True
        except Exception as e:
            logger.error(f"[Vision] Click failed at ({x}, {y}): {e}")
            return False
    
    async def click_element(self, vision_result: dict[str, Any]) -> bool:
        """
        Click an element identified by the vision model.
        
        Args:
            vision_result: Result from query_vision_model
            
        Returns:
            True if click succeeded
        """
        x = vision_result.get("x")
        y = vision_result.get("y")
        
        if x is None or y is None:
            logger.error("[Vision] No coordinates in vision result")
            return False
        
        return await self.click_by_coordinates(x, y)
    
    async def find_element_by_vision(self, goal: str) -> Optional[dict[str, Any]]:
        """
        Find an element using vision analysis.
        
        Complete workflow: screenshot -> mark -> query vision -> return result
        
        Args:
            goal: User's objective
            
        Returns:
            Vision result with coordinates, or None if not found
        """
        if not self._page:
            return None
        
        logger.info(f"[Vision] Starting vision-based search for: '{goal}'")
        
        # Take screenshot and mark it
        screenshot = await self.take_screenshot()
        await self.get_interactive_bounding_boxes()
        
        if not self._bounding_boxes:
            logger.warning("[Vision] No interactive elements found on page")
            return None
        
        marked_image = await self.apply_set_of_marks(screenshot)
        
        # Query vision model
        return await self.query_vision_model(goal, marked_image)
    
    async def describe_page(self) -> str:
        """
        Get a visual description of the current page.
        
        Returns:
            Text description of the page content
        """
        if not self._page:
            return "Not attached to any page"
        
        screenshot = await self.take_screenshot()
        image_base64 = base64.b64encode(screenshot).decode("utf-8")
        
        try:
            response = await self._openai.chat.completions.create(
                model=self._vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Describe this webpage briefly. What is the main purpose and what key interactive elements are visible?"
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_base64}",
                                    "detail": "low"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=500
            )
            
            return response.choices[0].message.content or "Could not describe page"
            
        except Exception as e:
            logger.error(f"[Vision] Error describing page: {e}")
            return f"Error: {e}"
    
    def get_mark_by_id(self, mark_id: int) -> Optional[BoundingBox]:
        """Get bounding box by mark ID."""
        for bbox in self._bounding_boxes:
            if bbox.mark_id == mark_id:
                return bbox
        return None
    
    async def get_marked_screenshot_base64(self) -> str:
        """Get the marked screenshot as base64 string."""
        marked_image = await self.apply_set_of_marks()
        return base64.b64encode(marked_image).decode("utf-8")
