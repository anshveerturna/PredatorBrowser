"""
Level 2: The Navigator (Blind Map)
Accessibility Tree parsing layer - Low cost.

This module uses the browser's Accessibility Tree to navigate
and interact with elements without relying on visual processing.
It converts the AX Tree to a simplified markdown format for
efficient LLM processing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from playwright.async_api import Page, ElementHandle, Locator
from openai import AsyncOpenAI

logger = logging.getLogger("predator.navigator")


@dataclass
class AXNode:
    """Represents a node in the Accessibility Tree."""
    role: str
    name: str
    node_id: int
    description: Optional[str] = None
    value: Optional[str] = None
    checked: Optional[bool] = None
    disabled: Optional[bool] = None
    expanded: Optional[bool] = None
    focused: Optional[bool] = None
    selected: Optional[bool] = None
    children: list["AXNode"] | None = None
    
    def to_markdown(self, indent: int = 0) -> str:
        """Convert node to simplified markdown representation."""
        prefix = "  " * indent
        
        # Build attributes string
        attrs = []
        if self.value:
            attrs.append(f'value="{self.value[:30]}..."' if len(str(self.value)) > 30 else f'value="{self.value}"')
        if self.disabled:
            attrs.append("disabled")
        if self.checked is not None:
            attrs.append(f"checked={self.checked}")
        if self.expanded is not None:
            attrs.append(f"expanded={self.expanded}")
        if self.focused:
            attrs.append("focused")
        if self.selected:
            attrs.append("selected")
        
        attrs_str = f" ({', '.join(attrs)})" if attrs else ""
        name_str = f' "{self.name}"' if self.name else ""
        
        line = f'{prefix}[{self.role}]{name_str} (ID: {self.node_id}){attrs_str}\n'
        
        if self.children:
            for child in self.children:
                line += child.to_markdown(indent + 1)
        
        return line


class Navigator:
    """
    Level 2: Blind Map - Accessibility Tree Navigator
    
    Uses the browser's Accessibility Tree to find and interact
    with elements. Converts the AX Tree to a simplified format
    for efficient LLM processing.
    """
    
    # Interactive roles we care about
    INTERACTIVE_ROLES = {
        "button", "link", "textbox", "searchbox", "combobox",
        "checkbox", "radio", "slider", "spinbutton", "switch",
        "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
        "option", "listbox", "tree", "treeitem", "gridcell"
    }
    
    # Content roles that might contain useful data
    CONTENT_ROLES = {
        "heading", "paragraph", "text", "StaticText", "listitem",
        "cell", "rowheader", "columnheader", "img", "figure"
    }
    
    def __init__(
        self,
        openai_client: AsyncOpenAI,
        model: str = "gpt-4o-mini"
    ) -> None:
        """
        Initialize the Navigator.
        
        Args:
            openai_client: AsyncOpenAI client for LLM decisions
            model: Model to use for AX Tree analysis
        """
        self._openai = openai_client
        self._model = model
        self._page: Optional[Page] = None
        self._node_counter = 0
        self._node_map: dict[int, dict[str, Any]] = {}
        
    async def attach(self, page: Page) -> None:
        """Attach navigator to a page."""
        self._page = page
        logger.info("[Navigator] Attached to page")
        
    def detach(self) -> None:
        """Detach navigator from page."""
        self._page = None
        self._node_map.clear()
        self._node_counter = 0
        logger.info("[Navigator] Detached from page")
        
    async def get_ax_tree(self, interesting_only: bool = True) -> Optional[AXNode]:
        """
        Get the Accessibility Tree from the page.
        
        Args:
            interesting_only: Filter to only interesting/interactive nodes
            
        Returns:
            Root AXNode of the tree
        """
        if not self._page:
            logger.error("[Navigator] Not attached to any page")
            return None
            
        self._node_counter = 0
        self._node_map.clear()
        
        try:
            snapshot = await self._page.accessibility.snapshot(interesting_only=interesting_only)
            if not snapshot:
                logger.warning("[Navigator] Empty accessibility snapshot")
                return None
            
            return self._convert_snapshot(snapshot)
        except Exception as e:
            logger.error(f"[Navigator] Failed to get AX tree: {e}")
            return None
    
    def _convert_snapshot(self, node: dict[str, Any]) -> AXNode:
        """Convert Playwright snapshot to AXNode."""
        self._node_counter += 1
        node_id = self._node_counter
        
        # Store mapping for later element lookup
        self._node_map[node_id] = node
        
        children = None
        if "children" in node:
            children = [self._convert_snapshot(child) for child in node["children"]]
        
        return AXNode(
            role=node.get("role", "unknown"),
            name=node.get("name", ""),
            node_id=node_id,
            description=node.get("description"),
            value=node.get("value"),
            checked=node.get("checked"),
            disabled=node.get("disabled"),
            expanded=node.get("expanded"),
            focused=node.get("focused"),
            selected=node.get("selected"),
            children=children
        )
    
    async def get_condensed_tree(self, max_nodes: int = 200) -> str:
        """
        Get a condensed markdown representation of the AX tree.
        
        Args:
            max_nodes: Maximum number of nodes to include
            
        Returns:
            Markdown string representation
        """
        root = await self.get_ax_tree(interesting_only=True)
        if not root:
            return "No accessibility tree available"
        
        markdown = root.to_markdown()
        
        # Truncate if too large
        lines = markdown.split("\n")
        if len(lines) > max_nodes:
            lines = lines[:max_nodes]
            lines.append(f"... (truncated, {len(lines)} of {self._node_counter} nodes shown)")
        
        return "\n".join(lines)
    
    async def find_element_by_ax(
        self,
        goal: str,
        action_type: str = "click"
    ) -> Optional[dict[str, Any]]:
        """
        Find an element in the AX tree that matches the goal.
        
        Args:
            goal: User's objective (e.g., "Click the submit button")
            action_type: Type of action to perform (click, type, etc.)
            
        Returns:
            Dict with element info and selector, or None if not found
        """
        if not self._page:
            logger.error("[Navigator] Not attached to any page")
            return None
            
        logger.info(f"[Navigator] Searching AX tree for: '{goal}'")
        
        # Get condensed tree
        tree_markdown = await self.get_condensed_tree()
        
        prompt = f"""Analyze this Accessibility Tree to find the element for the user's goal.

User Goal: {goal}
Action Type: {action_type}

Accessibility Tree:
{tree_markdown}

Instructions:
1. Find the element that best matches the goal
2. Consider the element's role, name, and context
3. For input actions, look for textbox, searchbox, or combobox roles
4. For click actions, look for button, link, or similar interactive roles
5. Return the exact ID number of the target element

Respond with JSON only:
{{
    "found": true/false,
    "element_id": <number or null>,
    "element_role": "<role>",
    "element_name": "<name>",
    "confidence": 0.0-1.0,
    "reasoning": "..."
}}"""

        try:
            response = await self._openai.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=500
            )
            
            result = json.loads(response.choices[0].message.content or "{}")
            
            if not result.get("found") or result.get("element_id") is None:
                logger.info(f"[Navigator] Element not found. Reason: {result.get('reasoning', 'N/A')}")
                return None
            
            element_id = result["element_id"]
            
            if element_id not in self._node_map:
                logger.warning(f"[Navigator] Element ID {element_id} not in node map")
                return None
            
            node_info = self._node_map[element_id]
            selector = await self._build_selector(node_info)
            
            logger.info(f"[Navigator] ✓ Found element: [{result.get('element_role')}] \"{result.get('element_name')}\" (ID: {element_id})")
            
            return {
                "level": "L2_AX_TREE",
                "element_id": element_id,
                "role": result.get("element_role"),
                "name": result.get("element_name"),
                "selector": selector,
                "confidence": result.get("confidence", 0.5),
                "node_info": node_info
            }
            
        except Exception as e:
            logger.error(f"[Navigator] Error during AX tree analysis: {e}")
            return None
    
    async def _build_selector(self, node_info: dict[str, Any]) -> str:
        """
        Build a Playwright selector from node info.
        
        Args:
            node_info: Node information from AX tree
            
        Returns:
            Playwright selector string
        """
        role = node_info.get("role", "")
        name = node_info.get("name", "")
        
        # Build role-based selector
        if role and name:
            # Use getByRole for semantic selection
            return f'role={role}[name="{name}"]'
        elif name:
            # Fallback to text-based selector
            return f'text="{name}"'
        elif role:
            return f'role={role}'
        
        return ""
    
    async def click_element(self, element_info: dict[str, Any]) -> bool:
        """
        Click an element found via AX tree.
        
        Args:
            element_info: Element info from find_element_by_ax
            
        Returns:
            True if click succeeded
        """
        if not self._page:
            return False
            
        selector = element_info.get("selector", "")
        if not selector:
            logger.error("[Navigator] No selector available")
            return False
            
        try:
            # Try the selector
            locator = self._page.locator(selector).first
            await locator.click(timeout=5000)
            logger.info(f"[Navigator] ✓ Clicked element with selector: {selector}")
            return True
        except Exception as e:
            logger.warning(f"[Navigator] Click failed with selector '{selector}': {e}")
            
            # Try alternative selectors
            node_info = element_info.get("node_info", {})
            name = node_info.get("name", "")
            
            if name:
                try:
                    await self._page.get_by_text(name, exact=False).first.click(timeout=5000)
                    logger.info(f"[Navigator] ✓ Clicked element using text: {name}")
                    return True
                except Exception as e2:
                    logger.warning(f"[Navigator] Text-based click also failed: {e2}")
            
            return False
    
    async def fill_element(self, element_info: dict[str, Any], text: str) -> bool:
        """
        Fill text into an element found via AX tree.
        
        Args:
            element_info: Element info from find_element_by_ax
            text: Text to enter
            
        Returns:
            True if fill succeeded
        """
        if not self._page:
            return False
            
        selector = element_info.get("selector", "")
        
        try:
            if selector:
                locator = self._page.locator(selector).first
                await locator.fill(text, timeout=5000)
                logger.info(f"[Navigator] ✓ Filled element with text")
                return True
        except Exception as e:
            logger.warning(f"[Navigator] Fill failed: {e}")
            
        # Try alternative approaches
        node_info = element_info.get("node_info", {})
        role = node_info.get("role", "")
        name = node_info.get("name", "")
        
        try:
            if role in ("textbox", "searchbox", "combobox"):
                if name:
                    await self._page.get_by_role(role, name=name).fill(text, timeout=5000)
                else:
                    await self._page.get_by_role(role).first.fill(text, timeout=5000)
                logger.info(f"[Navigator] ✓ Filled element using role selector")
                return True
        except Exception as e:
            logger.warning(f"[Navigator] Role-based fill failed: {e}")
            
        return False
    
    async def extract_text_content(self, goal: str) -> Optional[dict[str, Any]]:
        """
        Extract text content from the page matching a goal.
        
        Args:
            goal: What text to extract (e.g., "the price")
            
        Returns:
            Extracted text data or None
        """
        if not self._page:
            return None
            
        logger.info(f"[Navigator] Extracting text for: '{goal}'")
        
        tree_markdown = await self.get_condensed_tree()
        
        prompt = f"""Extract the requested information from this Accessibility Tree.

Goal: {goal}

Accessibility Tree:
{tree_markdown}

Instructions:
1. Find all text content relevant to the goal
2. Include context (labels, headings) for clarity
3. Extract the actual values/text, not just the element IDs

Respond with JSON:
{{
    "found": true/false,
    "data": <extracted text or structured data>,
    "source_elements": [<list of element IDs used>],
    "confidence": 0.0-1.0
}}"""

        try:
            response = await self._openai.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=1000
            )
            
            result = json.loads(response.choices[0].message.content or "{}")
            
            if result.get("found"):
                logger.info("[Navigator] ✓ Extracted text content from AX tree")
                return {
                    "level": "L2_AX_TREE",
                    "source": "accessibility_tree",
                    "data": result.get("data"),
                    "confidence": result.get("confidence", 0.5)
                }
            
            return None
            
        except Exception as e:
            logger.error(f"[Navigator] Error during text extraction: {e}")
            return None
    
    async def get_interactive_elements(self) -> list[dict[str, Any]]:
        """
        Get all interactive elements from the page.
        
        Returns:
            List of interactive element info
        """
        root = await self.get_ax_tree(interesting_only=True)
        if not root:
            return []
        
        elements = []
        
        def collect_interactive(node: AXNode) -> None:
            if node.role.lower() in self.INTERACTIVE_ROLES:
                elements.append({
                    "id": node.node_id,
                    "role": node.role,
                    "name": node.name,
                    "disabled": node.disabled,
                    "value": node.value
                })
            if node.children:
                for child in node.children:
                    collect_interactive(child)
        
        collect_interactive(root)
        return elements
