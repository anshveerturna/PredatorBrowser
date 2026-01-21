"""
PredatorBrowser - The Main Browser Orchestrator

This is the core class that implements the Waterfall Cost-Logic:
1. Level 1 (Shadow API): Network interception - 0 cost, max speed
2. Level 2 (Blind Map): Accessibility Tree - Low cost
3. Level 3 (Eagle Eye): Vision with Set-of-Marks - High cost

The Predator Browser does NOT act like a standard scraper.
It is an information interceptor.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncGenerator, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError
)
from openai import AsyncOpenAI

from app.core.levels.sniffer import Sniffer
from app.core.levels.navigator import Navigator
from app.core.levels.vision import VisionEngine

logger = logging.getLogger("predator")


class WaterfallLevel(Enum):
    """Waterfall execution levels."""
    L1_SHADOW_API = "L1_SHADOW_API"
    L2_AX_TREE = "L2_AX_TREE"
    L3_VISION = "L3_VISION"
    FAILED = "FAILED"


@dataclass
class ExecutionResult:
    """Result of a Predator Browser execution."""
    success: bool
    level: WaterfallLevel
    data: Any = None
    action_taken: str = ""
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    url_changed: bool = False
    dom_changed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "level": self.level.value,
            "data": self.data,
            "action_taken": self.action_taken,
            "error": self.error,
            "execution_time_ms": self.execution_time_ms,
            "url_changed": self.url_changed,
            "dom_changed": self.dom_changed,
            "metadata": self.metadata
        }


@dataclass
class BrowserConfig:
    """Configuration for PredatorBrowser."""
    headless: bool = True
    viewport_width: int = 1920
    viewport_height: int = 1080
    user_agent: Optional[str] = None
    locale: str = "en-US"
    timezone_id: str = "America/New_York"
    default_timeout: int = 30000
    stealth_mode: bool = True
    proxy: Optional[dict[str, str]] = None
    
    # LLM Configuration
    openai_api_key: Optional[str] = None
    router_model: str = "gpt-4o-mini"  # Fast model for routing
    vision_model: str = "gpt-4o"  # Vision model for Level 3


class PredatorBrowser:
    """
    The Predator Browser - Enterprise Agentic Browser Module
    
    An information interceptor that follows strict Waterfall Cost-Logic
    for every interaction to maximize speed and reliability while
    minimizing token costs.
    """
    
    # Stealth headers to avoid bot detection
    STEALTH_HEADERS = {
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    
    # Default stealth user agent
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    
    def __init__(self, config: Optional[BrowserConfig] = None) -> None:
        """
        Initialize PredatorBrowser.
        
        Args:
            config: Browser configuration, uses defaults if not provided
        """
        self.config = config or BrowserConfig()
        
        # Playwright instances
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        
        # OpenAI client
        api_key = self.config.openai_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OpenAI API key required. Set OPENAI_API_KEY environment variable or pass in config.")
        self._openai = AsyncOpenAI(api_key=api_key)
        
        # Waterfall levels
        self._sniffer: Optional[Sniffer] = None
        self._navigator: Optional[Navigator] = None
        self._vision: Optional[VisionEngine] = None
        
        # State tracking
        self._initialized = False
        self._current_url: str = ""
        self._last_dom_hash: str = ""
        
        logger.info("[Predator] Browser instance created")
    
    async def initialize(self) -> None:
        """Initialize the browser and all components."""
        if self._initialized:
            logger.warning("[Predator] Already initialized")
            return
        
        logger.info("[Predator] Initializing browser...")
        
        # Start Playwright
        self._playwright = await async_playwright().start()
        
        # Launch browser with stealth options
        launch_options: dict[str, Any] = {
            "headless": self.config.headless,
        }
        
        if self.config.proxy:
            launch_options["proxy"] = self.config.proxy
        
        self._browser = await self._playwright.chromium.launch(**launch_options)
        
        # Create context with stealth configuration
        context_options: dict[str, Any] = {
            "viewport": {
                "width": self.config.viewport_width,
                "height": self.config.viewport_height
            },
            "locale": self.config.locale,
            "timezone_id": self.config.timezone_id,
        }
        
        if self.config.stealth_mode:
            context_options["user_agent"] = self.config.user_agent or self.DEFAULT_USER_AGENT
            context_options["extra_http_headers"] = self.STEALTH_HEADERS
            # Stealth: Mask automation indicators
            context_options["bypass_csp"] = True
        
        self._context = await self._browser.new_context(**context_options)
        
        # Apply additional stealth measures
        if self.config.stealth_mode:
            await self._apply_stealth_scripts()
        
        # Create main page
        self._page = await self._context.new_page()
        await self._page.set_default_timeout(self.config.default_timeout)
        
        # Initialize waterfall levels
        self._sniffer = Sniffer(
            self._openai,
            router_model=self.config.router_model
        )
        await self._sniffer.attach(self._page)
        
        self._navigator = Navigator(
            self._openai,
            model=self.config.router_model
        )
        await self._navigator.attach(self._page)
        
        self._vision = VisionEngine(
            self._openai,
            vision_model=self.config.vision_model
        )
        await self._vision.attach(self._page)
        
        self._initialized = True
        logger.info("[Predator] ✓ Browser initialized successfully")
    
    async def _apply_stealth_scripts(self) -> None:
        """Apply stealth JavaScript to evade bot detection."""
        if not self._context:
            return
        
        stealth_js = """
        // Override webdriver property
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
        
        // Override plugins
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
        
        // Override languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });
        
        // Override chrome property
        window.chrome = {
            runtime: {}
        };
        
        // Override permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
        );
        
        // Spoof WebGL renderer
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) {
                return 'Intel Inc.';
            }
            if (parameter === 37446) {
                return 'Intel Iris OpenGL Engine';
            }
            return getParameter.call(this, parameter);
        };
        """
        
        await self._context.add_init_script(stealth_js)
        logger.debug("[Predator] Stealth scripts applied")
    
    async def close(self) -> None:
        """Close the browser and cleanup resources."""
        logger.info("[Predator] Closing browser...")
        
        if self._sniffer:
            await self._sniffer.detach()
        if self._navigator:
            self._navigator.detach()
        if self._vision:
            self._vision.detach()
        
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        
        self._initialized = False
        logger.info("[Predator] ✓ Browser closed")
    
    @asynccontextmanager
    async def session(self) -> AsyncGenerator["PredatorBrowser", None]:
        """Context manager for browser session."""
        await self.initialize()
        try:
            yield self
        finally:
            await self.close()
    
    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> bool:
        """
        Navigate to a URL.
        
        Args:
            url: URL to navigate to
            wait_until: Wait condition (load, domcontentloaded, networkidle)
            
        Returns:
            True if navigation succeeded
        """
        if not self._page:
            raise RuntimeError("Browser not initialized")
        
        logger.info(f"[Predator] Navigating to: {url}")
        
        # Clear sniffer buffer for fresh capture
        if self._sniffer:
            self._sniffer.clear_buffer()
        
        try:
            await self._page.goto(url, wait_until=wait_until)
            self._current_url = self._page.url
            
            # Wait a bit for any API calls to complete
            await asyncio.sleep(1)
            
            logger.info(f"[Predator] ✓ Navigation complete: {self._current_url}")
            return True
            
        except PlaywrightTimeout as e:
            logger.error(f"[Predator] Navigation timeout: {e}")
            return False
        except PlaywrightError as e:
            logger.error(f"[Predator] Navigation error: {e}")
            return False
    
    async def _get_dom_hash(self) -> str:
        """Get a hash of the current DOM state for change detection."""
        if not self._page:
            return ""
        
        try:
            html = await self._page.content()
            return str(hash(html))
        except Exception:
            return ""
    
    async def _check_state_changes(self, original_url: str, original_dom_hash: str) -> tuple[bool, bool]:
        """Check if URL or DOM changed after an action."""
        if not self._page:
            return False, False
        
        await asyncio.sleep(0.5)  # Wait for any state changes
        
        current_url = self._page.url
        current_dom_hash = await self._get_dom_hash()
        
        url_changed = current_url != original_url
        dom_changed = current_dom_hash != original_dom_hash
        
        return url_changed, dom_changed
    
    async def execute_goal(self, goal: str) -> ExecutionResult:
        """
        Execute a goal using the Waterfall Cost-Logic.
        
        This is the main entry point for agentic interactions.
        Tries each level in order until success or all levels exhausted.
        
        Args:
            goal: The user's objective (e.g., "Find the price of the flight")
            
        Returns:
            ExecutionResult with data or action confirmation
        """
        if not self._initialized:
            raise RuntimeError("Browser not initialized. Call initialize() first.")
        
        start_time = datetime.now()
        logger.info(f"[Predator] ═══ Executing Goal: '{goal}' ═══")
        
        original_url = self._page.url if self._page else ""
        original_dom_hash = await self._get_dom_hash()
        
        # ═══ Level 1: Shadow API (Network Interception) ═══
        logger.info("[Predator] → Level 1: Checking Shadow API (Network Traffic)...")
        
        if self._sniffer:
            try:
                l1_result = await self._sniffer.analyze_traffic(goal)
                if l1_result:
                    elapsed = (datetime.now() - start_time).total_seconds() * 1000
                    logger.info(f"[Predator] ✓ Goal achieved at Level 1 in {elapsed:.0f}ms")
                    return ExecutionResult(
                        success=True,
                        level=WaterfallLevel.L1_SHADOW_API,
                        data=l1_result.get("data"),
                        action_taken="Extracted from network traffic",
                        execution_time_ms=elapsed,
                        metadata={"source_url": l1_result.get("source_url")}
                    )
            except Exception as e:
                logger.warning(f"[Predator] Level 1 error: {e}")
        
        logger.info("[Predator] Level 1 Failed. Promoting to Level 2...")
        
        # ═══ Level 2: Blind Map (Accessibility Tree) ═══
        logger.info("[Predator] → Level 2: Checking Accessibility Tree...")
        
        if self._navigator:
            try:
                # First try to extract data directly from AX tree
                l2_extraction = await self._navigator.extract_text_content(goal)
                if l2_extraction:
                    elapsed = (datetime.now() - start_time).total_seconds() * 1000
                    logger.info(f"[Predator] ✓ Goal achieved at Level 2 (extraction) in {elapsed:.0f}ms")
                    return ExecutionResult(
                        success=True,
                        level=WaterfallLevel.L2_AX_TREE,
                        data=l2_extraction.get("data"),
                        action_taken="Extracted from Accessibility Tree",
                        execution_time_ms=elapsed
                    )
                
                # If extraction fails, try to find an interactive element
                l2_element = await self._navigator.find_element_by_ax(goal)
                if l2_element:
                    # Click the element
                    click_success = await self._navigator.click_element(l2_element)
                    if click_success:
                        url_changed, dom_changed = await self._check_state_changes(original_url, original_dom_hash)
                        elapsed = (datetime.now() - start_time).total_seconds() * 1000
                        
                        logger.info(f"[Predator] ✓ Goal achieved at Level 2 (click) in {elapsed:.0f}ms")
                        return ExecutionResult(
                            success=True,
                            level=WaterfallLevel.L2_AX_TREE,
                            action_taken=f"Clicked [{l2_element.get('role')}] \"{l2_element.get('name')}\"",
                            execution_time_ms=elapsed,
                            url_changed=url_changed,
                            dom_changed=dom_changed,
                            metadata={"element": l2_element}
                        )
                        
            except Exception as e:
                logger.warning(f"[Predator] Level 2 error: {e}")
        
        logger.info("[Predator] Level 2 Failed. Promoting to Level 3...")
        
        # ═══ Level 3: Eagle Eye (Vision + Set-of-Marks) ═══
        logger.info("[Predator] → Level 3: Activating Vision Engine...")
        
        if self._vision:
            try:
                l3_result = await self._vision.find_element_by_vision(goal)
                if l3_result:
                    # Click using coordinates
                    click_success = await self._vision.click_element(l3_result)
                    if click_success:
                        url_changed, dom_changed = await self._check_state_changes(original_url, original_dom_hash)
                        elapsed = (datetime.now() - start_time).total_seconds() * 1000
                        
                        logger.info(f"[Predator] ✓ Goal achieved at Level 3 in {elapsed:.0f}ms")
                        return ExecutionResult(
                            success=True,
                            level=WaterfallLevel.L3_VISION,
                            action_taken=f"Clicked element at ({l3_result['x']:.0f}, {l3_result['y']:.0f})",
                            execution_time_ms=elapsed,
                            url_changed=url_changed,
                            dom_changed=dom_changed,
                            metadata={"vision_result": l3_result}
                        )
                        
            except Exception as e:
                logger.warning(f"[Predator] Level 3 error: {e}")
        
        # All levels failed
        elapsed = (datetime.now() - start_time).total_seconds() * 1000
        logger.error(f"[Predator] ✗ All levels exhausted. Goal could not be achieved.")
        
        return ExecutionResult(
            success=False,
            level=WaterfallLevel.FAILED,
            error="Could not achieve goal at any level",
            execution_time_ms=elapsed
        )
    
    async def click(self, element_description: str) -> ExecutionResult:
        """
        Smart click using Level 2 first, then Level 3.
        
        Args:
            element_description: Description of element to click
            
        Returns:
            ExecutionResult with action confirmation
        """
        if not self._initialized:
            raise RuntimeError("Browser not initialized")
        
        start_time = datetime.now()
        logger.info(f"[Predator] Click requested: '{element_description}'")
        
        original_url = self._page.url if self._page else ""
        original_dom_hash = await self._get_dom_hash()
        
        # Try Level 2 first
        if self._navigator:
            element = await self._navigator.find_element_by_ax(element_description, action_type="click")
            if element:
                success = await self._navigator.click_element(element)
                if success:
                    url_changed, dom_changed = await self._check_state_changes(original_url, original_dom_hash)
                    elapsed = (datetime.now() - start_time).total_seconds() * 1000
                    return ExecutionResult(
                        success=True,
                        level=WaterfallLevel.L2_AX_TREE,
                        action_taken=f"Clicked [{element.get('role')}] \"{element.get('name')}\"",
                        execution_time_ms=elapsed,
                        url_changed=url_changed,
                        dom_changed=dom_changed
                    )
        
        # Fallback to Level 3
        logger.info("[Predator] Level 2 click failed. Trying Vision...")
        
        if self._vision:
            vision_result = await self._vision.find_element_by_vision(element_description)
            if vision_result:
                success = await self._vision.click_element(vision_result)
                if success:
                    url_changed, dom_changed = await self._check_state_changes(original_url, original_dom_hash)
                    elapsed = (datetime.now() - start_time).total_seconds() * 1000
                    return ExecutionResult(
                        success=True,
                        level=WaterfallLevel.L3_VISION,
                        action_taken=f"Clicked at ({vision_result['x']:.0f}, {vision_result['y']:.0f})",
                        execution_time_ms=elapsed,
                        url_changed=url_changed,
                        dom_changed=dom_changed
                    )
        
        elapsed = (datetime.now() - start_time).total_seconds() * 1000
        return ExecutionResult(
            success=False,
            level=WaterfallLevel.FAILED,
            error=f"Could not click element: {element_description}",
            execution_time_ms=elapsed
        )
    
    async def type_text(self, field_description: str, text: str) -> ExecutionResult:
        """
        Type text into a field using AX Tree navigation.
        
        Args:
            field_description: Description of the input field
            text: Text to type
            
        Returns:
            ExecutionResult with action confirmation
        """
        if not self._initialized:
            raise RuntimeError("Browser not initialized")
        
        start_time = datetime.now()
        logger.info(f"[Predator] Type requested: '{text}' into '{field_description}'")
        
        # Try Level 2
        if self._navigator:
            element = await self._navigator.find_element_by_ax(field_description, action_type="type")
            if element:
                success = await self._navigator.fill_element(element, text)
                if success:
                    elapsed = (datetime.now() - start_time).total_seconds() * 1000
                    return ExecutionResult(
                        success=True,
                        level=WaterfallLevel.L2_AX_TREE,
                        action_taken=f"Typed into [{element.get('role')}] \"{element.get('name')}\"",
                        execution_time_ms=elapsed
                    )
        
        # Fallback: Try direct page methods
        if self._page:
            try:
                # Try to find by placeholder, label, or role
                locators = [
                    self._page.get_by_placeholder(field_description),
                    self._page.get_by_label(field_description),
                    self._page.get_by_role("textbox", name=field_description),
                    self._page.get_by_role("searchbox", name=field_description),
                ]
                
                for locator in locators:
                    try:
                        if await locator.count() > 0:
                            await locator.first.fill(text, timeout=5000)
                            elapsed = (datetime.now() - start_time).total_seconds() * 1000
                            return ExecutionResult(
                                success=True,
                                level=WaterfallLevel.L2_AX_TREE,
                                action_taken=f"Typed text using fallback locator",
                                execution_time_ms=elapsed
                            )
                    except Exception:
                        continue
                        
            except Exception as e:
                logger.warning(f"[Predator] Fallback type failed: {e}")
        
        elapsed = (datetime.now() - start_time).total_seconds() * 1000
        return ExecutionResult(
            success=False,
            level=WaterfallLevel.FAILED,
            error=f"Could not type into field: {field_description}",
            execution_time_ms=elapsed
        )
    
    async def extract_data(self, schema: dict[str, Any]) -> ExecutionResult:
        """
        Extract structured data from the page.
        
        Prioritizes Shadow API (Level 1), then falls back to AX Tree.
        
        Args:
            schema: Expected data schema (e.g., {"title": "string", "price": "number"})
            
        Returns:
            ExecutionResult with extracted data
        """
        if not self._initialized:
            raise RuntimeError("Browser not initialized")
        
        start_time = datetime.now()
        logger.info(f"[Predator] Data extraction requested: {schema}")
        
        # Level 1: Try to find data in network traffic
        if self._sniffer:
            goal = f"Extract data matching schema: {schema}"
            l1_result = await self._sniffer.analyze_traffic(goal)
            if l1_result:
                elapsed = (datetime.now() - start_time).total_seconds() * 1000
                return ExecutionResult(
                    success=True,
                    level=WaterfallLevel.L1_SHADOW_API,
                    data=l1_result.get("data"),
                    action_taken="Extracted from network traffic",
                    execution_time_ms=elapsed
                )
        
        # Level 2: Extract from AX Tree
        if self._navigator:
            # Build extraction goal from schema
            fields = ", ".join(schema.keys())
            goal = f"Extract the following fields: {fields}"
            
            l2_result = await self._navigator.extract_text_content(goal)
            if l2_result:
                elapsed = (datetime.now() - start_time).total_seconds() * 1000
                return ExecutionResult(
                    success=True,
                    level=WaterfallLevel.L2_AX_TREE,
                    data=l2_result.get("data"),
                    action_taken="Extracted from Accessibility Tree",
                    execution_time_ms=elapsed
                )
        
        # Level 3: Use vision to extract data (expensive)
        if self._vision and self._page:
            try:
                description = await self._vision.describe_page()
                
                # Use LLM to extract structured data from description
                prompt = f"""Extract structured data from this page description.

Page Description:
{description}

Required Schema:
{schema}

Extract the data and return as JSON matching the schema. If a field cannot be found, use null."""

                response = await self._openai.chat.completions.create(
                    model=self.config.router_model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0
                )
                
                import json
                data = json.loads(response.choices[0].message.content or "{}")
                
                elapsed = (datetime.now() - start_time).total_seconds() * 1000
                return ExecutionResult(
                    success=True,
                    level=WaterfallLevel.L3_VISION,
                    data=data,
                    action_taken="Extracted using vision analysis",
                    execution_time_ms=elapsed
                )
                
            except Exception as e:
                logger.error(f"[Predator] Vision extraction failed: {e}")
        
        elapsed = (datetime.now() - start_time).total_seconds() * 1000
        return ExecutionResult(
            success=False,
            level=WaterfallLevel.FAILED,
            error="Could not extract data from page",
            execution_time_ms=elapsed
        )
    
    async def get_page_info(self) -> dict[str, Any]:
        """Get current page information."""
        if not self._page:
            return {"error": "No page loaded"}
        
        return {
            "url": self._page.url,
            "title": await self._page.title(),
            "viewport": self._page.viewport_size
        }
    
    async def screenshot(self, full_page: bool = False) -> bytes:
        """Take a screenshot of the current page."""
        if not self._page:
            raise RuntimeError("No page loaded")
        return await self._page.screenshot(full_page=full_page, type="png")
    
    async def get_marked_screenshot(self) -> str:
        """Get a Set-of-Marks annotated screenshot as base64."""
        if not self._vision:
            raise RuntimeError("Vision engine not initialized")
        return await self._vision.get_marked_screenshot_base64()
    
    @property
    def page(self) -> Optional[Page]:
        """Direct access to the Playwright page for advanced usage."""
        return self._page
    
    @property
    def sniffer(self) -> Optional[Sniffer]:
        """Access to the Sniffer component."""
        return self._sniffer
    
    @property
    def navigator(self) -> Optional[Navigator]:
        """Access to the Navigator component."""
        return self._navigator
    
    @property
    def vision(self) -> Optional[VisionEngine]:
        """Access to the Vision Engine component."""
        return self._vision
