"""
Level 1: The Sniffer (Shadow API)
Network interception layer - 0 cost, maximum speed.

This module intercepts and analyzes network traffic to extract data
without touching the DOM, providing the fastest and most reliable
data extraction method.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Optional, Callable, Awaitable

from playwright.async_api import Page, Response
from openai import AsyncOpenAI

logger = logging.getLogger("predator.sniffer")


@dataclass
class NetworkCapture:
    """Represents a captured network response."""
    url: str
    status: int
    content_type: str
    body: dict[str, Any] | list[Any] | None
    timestamp: datetime = field(default_factory=datetime.now)
    headers: dict[str, str] = field(default_factory=dict)
    
    def get_structure(self, max_depth: int = 3) -> dict[str, Any]:
        """Extract the structure/schema of the JSON body."""
        def extract_schema(obj: Any, depth: int = 0) -> Any:
            if depth >= max_depth:
                return "..."
            if isinstance(obj, dict):
                return {k: extract_schema(v, depth + 1) for k, v in list(obj.items())[:20]}
            elif isinstance(obj, list):
                if obj:
                    return [extract_schema(obj[0], depth + 1)]
                return []
            elif isinstance(obj, str):
                return f"str({len(obj)})"
            elif isinstance(obj, (int, float)):
                return type(obj).__name__
            elif isinstance(obj, bool):
                return "bool"
            elif obj is None:
                return "null"
            return str(type(obj).__name__)
        
        return extract_schema(self.body)


class Sniffer:
    """
    Level 1: Shadow API - Network Traffic Interceptor
    
    Listens to all network responses and maintains a rolling buffer
    of JSON responses for analysis. Uses a fast LLM to determine if
    captured data matches the user's goal.
    """
    
    def __init__(
        self,
        openai_client: AsyncOpenAI,
        buffer_size: int = 50,
        router_model: str = "gpt-4o-mini"
    ) -> None:
        """
        Initialize the Sniffer.
        
        Args:
            openai_client: AsyncOpenAI client for LLM routing
            buffer_size: Maximum number of responses to keep in buffer
            router_model: Fast model for routing decisions
        """
        self._openai = openai_client
        self._buffer: Deque[NetworkCapture] = deque(maxlen=buffer_size)
        self._router_model = router_model
        self._active = False
        self._page: Optional[Page] = None
        self._handler: Optional[Callable[[Response], Awaitable[None]]] = None
        
    async def attach(self, page: Page) -> None:
        """
        Attach the sniffer to a Playwright page.
        
        Args:
            page: Playwright Page instance to monitor
        """
        self._page = page
        self._active = True
        
        async def response_handler(response: Response) -> None:
            await self._capture_response(response)
        
        self._handler = response_handler
        page.on("response", self._handler)
        logger.info("[Sniffer] Attached to page and listening for responses")
        
    async def detach(self) -> None:
        """Detach the sniffer from the page."""
        if self._page and self._handler:
            self._page.remove_listener("response", self._handler)
            self._active = False
            self._page = None
            self._handler = None
            logger.info("[Sniffer] Detached from page")
    
    async def _capture_response(self, response: Response) -> None:
        """
        Capture and store a network response if it's JSON.
        
        Args:
            response: Playwright Response object
        """
        if not self._active:
            return
            
        content_type = response.headers.get("content-type", "")
        
        # Only capture JSON responses
        if "application/json" not in content_type:
            return
            
        try:
            body = await response.json()
            capture = NetworkCapture(
                url=response.url,
                status=response.status,
                content_type=content_type,
                body=body,
                headers=dict(response.headers)
            )
            self._buffer.append(capture)
            logger.debug(f"[Sniffer] Captured JSON from: {response.url[:80]}...")
        except Exception as e:
            logger.debug(f"[Sniffer] Failed to parse JSON from {response.url}: {e}")
    
    def clear_buffer(self) -> None:
        """Clear the response buffer."""
        self._buffer.clear()
        logger.debug("[Sniffer] Buffer cleared")
    
    def get_buffer_summary(self) -> list[dict[str, Any]]:
        """
        Get a summary of all captured responses.
        
        Returns:
            List of capture summaries with URL, structure, and timestamp
        """
        return [
            {
                "url": cap.url,
                "status": cap.status,
                "structure": cap.get_structure(),
                "timestamp": cap.timestamp.isoformat()
            }
            for cap in self._buffer
        ]
    
    async def analyze_traffic(self, goal: str) -> Optional[dict[str, Any]]:
        """
        Analyze captured traffic to find data matching the goal.
        
        Uses a fast router LLM to determine if any captured JSON
        contains the answer to the user's goal.
        
        Args:
            goal: The user's objective (e.g., "Find the flight price")
            
        Returns:
            Extracted data if found, None otherwise
        """
        if not self._buffer:
            logger.info("[Sniffer] No captured responses to analyze")
            return None
            
        logger.info(f"[Sniffer] Analyzing {len(self._buffer)} captured responses for goal: '{goal}'")
        
        # Build a summary of captured JSON structures
        captures_summary = []
        for idx, capture in enumerate(self._buffer):
            captures_summary.append({
                "index": idx,
                "url": capture.url,
                "structure": capture.get_structure(max_depth=2)
            })
        
        # Ask the router LLM to identify relevant captures
        router_prompt = f"""You are analyzing network traffic to find data matching a user's goal.

Goal: {goal}

Captured JSON Responses (showing structure only):
{json.dumps(captures_summary, indent=2)}

Instructions:
1. Analyze each captured response structure
2. Identify which response(s) might contain the answer to the goal
3. If found, return the index number(s) of relevant captures
4. If no relevant data found, return an empty list

Respond with JSON only: {{"relevant_indices": [0, 1], "reasoning": "..."}} or {{"relevant_indices": [], "reasoning": "..."}}"""

        try:
            response = await self._openai.chat.completions.create(
                model=self._router_model,
                messages=[{"role": "user", "content": router_prompt}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=500
            )
            
            result = json.loads(response.choices[0].message.content or "{}")
            relevant_indices = result.get("relevant_indices", [])
            
            if not relevant_indices:
                logger.info(f"[Sniffer] No relevant data found. Reason: {result.get('reasoning', 'N/A')}")
                return None
                
            logger.info(f"[Sniffer] Found relevant captures at indices: {relevant_indices}")
            
            # Extract actual data from relevant captures
            relevant_data = []
            for idx in relevant_indices:
                if 0 <= idx < len(self._buffer):
                    capture = list(self._buffer)[idx]
                    relevant_data.append({
                        "url": capture.url,
                        "data": capture.body
                    })
            
            # Use LLM to extract the specific answer
            extraction_prompt = f"""Extract the answer to this goal from the provided JSON data.

Goal: {goal}

JSON Data:
{json.dumps(relevant_data, indent=2, default=str)[:8000]}

Instructions:
1. Find the specific data that answers the goal
2. Extract and format it clearly
3. Include relevant context (e.g., currency for prices)

Respond with JSON: {{"found": true/false, "data": <extracted_data>, "source_url": "..."}}"""

            extraction_response = await self._openai.chat.completions.create(
                model=self._router_model,
                messages=[{"role": "user", "content": extraction_prompt}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=2000
            )
            
            extraction_result = json.loads(extraction_response.choices[0].message.content or "{}")
            
            if extraction_result.get("found"):
                logger.info("[Sniffer] ✓ Successfully extracted data from Shadow API")
                return {
                    "level": "L1_SHADOW_API",
                    "source": "network_traffic",
                    "data": extraction_result.get("data"),
                    "source_url": extraction_result.get("source_url")
                }
            else:
                logger.info("[Sniffer] Data found but could not extract specific answer")
                return None
                
        except Exception as e:
            logger.error(f"[Sniffer] Error during traffic analysis: {e}")
            return None
    
    async def search_for_pattern(self, pattern: str) -> list[NetworkCapture]:
        """
        Search captured responses for a specific pattern.
        
        Args:
            pattern: Text pattern to search for in URLs or response bodies
            
        Returns:
            List of matching captures
        """
        matches = []
        pattern_lower = pattern.lower()
        
        for capture in self._buffer:
            # Check URL
            if pattern_lower in capture.url.lower():
                matches.append(capture)
                continue
            
            # Check body content
            if capture.body:
                body_str = json.dumps(capture.body).lower()
                if pattern_lower in body_str:
                    matches.append(capture)
        
        return matches
    
    async def wait_for_api_response(
        self,
        url_pattern: str,
        timeout: float = 10.0
    ) -> Optional[NetworkCapture]:
        """
        Wait for a specific API response matching the URL pattern.
        
        Args:
            url_pattern: Substring to match in response URLs
            timeout: Maximum time to wait in seconds
            
        Returns:
            Matching capture if found, None on timeout
        """
        start_time = asyncio.get_event_loop().time()
        initial_count = len(self._buffer)
        
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            # Check new captures
            for capture in list(self._buffer)[initial_count:]:
                if url_pattern in capture.url:
                    logger.info(f"[Sniffer] Found matching API response: {capture.url}")
                    return capture
            
            await asyncio.sleep(0.1)
        
        logger.warning(f"[Sniffer] Timeout waiting for API response matching: {url_pattern}")
        return None
