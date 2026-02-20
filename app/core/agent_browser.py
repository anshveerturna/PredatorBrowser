"""
AgentBrowser — Clean agentic browser for LLM control.

Exposes simple primitives: screenshot, get_state, click, type, scroll, etc.
The calling LLM is the brain; this browser is the hands and eyes.

No external LLM API needed. No hardcoded selectors.
Uses Playwright with stealth configuration.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urlparse

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
)

logger = logging.getLogger("predator.agent")


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

@dataclass
class AgentBrowserConfig:
    # CDP connection: connect to an already-running Chrome (best anti-detection)
    cdp_url: Optional[str] = None  # e.g. "http://localhost:9222"

    # If no cdp_url, auto-launch Chrome with remote debugging on this port (0 = find free port)
    auto_launch_debug_port: int = 0

    # User data dir for auto-launched Chrome (uses real profile = real cookies/extensions)
    user_data_dir: Optional[str] = None  # e.g. path to a Chrome profile dir

    headless: bool = False  # Default headed for anti-detection
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "en-US"
    timezone_id: str = "America/New_York"
    default_timeout: int = 30000
    stealth_mode: bool = True
    proxy: Optional[dict[str, str]] = None


# ─────────────────────────────────────────────
# Stealth
# ─────────────────────────────────────────────

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en']
});
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) => (
    params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : origQuery(params)
);
const getParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return getParam.call(this, p);
};
"""

STEALTH_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# ─────────────────────────────────────────────
# State Extraction (lightweight)
# ─────────────────────────────────────────────

EXTRACT_ELEMENTS_JS = """
() => {
    const INTERACTIVE = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="checkbox"], [role="radio"], [role="switch"], [role="combobox"], [role="searchbox"], [role="textbox"], [onclick], [tabindex]';
    const results = [];
    let id = 1;
    for (const el of document.querySelectorAll(INTERACTIVE)) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        const visible = rect.top < window.innerHeight && rect.bottom > 0
                     && rect.left < window.innerWidth && rect.right > 0;
        if (!visible) continue;
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || tag;
        let name = el.getAttribute('aria-label')
                || el.getAttribute('title')
                || el.getAttribute('alt')
                || el.getAttribute('placeholder')
                || el.innerText?.trim().substring(0, 80)
                || el.getAttribute('name')
                || el.getAttribute('id')
                || '';
        name = name.replace(/\\s+/g, ' ').trim().substring(0, 100);
        const entry = {
            id: id++,
            tag: tag,
            role: role,
            name: name,
            bbox: {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
            },
            enabled: !el.disabled,
            visible: true,
        };
        if (tag === 'input' || tag === 'textarea') {
            entry.type = el.type || 'text';
            entry.value = el.value || '';
        }
        if (tag === 'select') {
            entry.type = 'select';
            entry.value = el.value || '';
            entry.options = Array.from(el.options).slice(0, 20).map(o => ({
                value: o.value, text: o.text.trim().substring(0, 60),
                selected: o.selected
            }));
        }
        if (tag === 'a') {
            entry.href = el.href || '';
        }
        if (el.getAttribute('aria-checked') !== null || el.checked !== undefined) {
            entry.checked = el.checked || el.getAttribute('aria-checked') === 'true';
        }
        entry.focused = document.activeElement === el;
        results.push(entry);
        if (results.length >= 80) break;
    }
    return results;
}
"""

GET_SCROLL_INFO_JS = """
() => ({
    x: window.scrollX,
    y: window.scrollY,
    maxX: document.documentElement.scrollWidth - window.innerWidth,
    maxY: document.documentElement.scrollHeight - window.innerHeight,
    pageWidth: document.documentElement.scrollWidth,
    pageHeight: document.documentElement.scrollHeight,
})
"""

GET_PAGE_TEXT_JS = """
(maxLen) => {
    const walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_TEXT, null
    );
    const parts = [];
    let total = 0;
    while (walker.nextNode()) {
        const text = walker.currentNode.textContent.trim();
        if (text.length > 1) {
            parts.push(text);
            total += text.length;
            if (total > maxLen) break;
        }
    }
    return parts.join(' ').substring(0, maxLen);
}
"""


# ─────────────────────────────────────────────
# AgentBrowser
# ─────────────────────────────────────────────

class AgentBrowser:
    """
    Agentic browser for LLM control.

    Exposes simple primitives. The calling LLM is the brain.

    Connection modes (in priority order):
      1. CDP connect — attach to an already-running Chrome (best anti-detection)
      2. Auto-launch — start Chrome with --remote-debugging-port, then connect via CDP
      3. Fallback — Playwright launch (detectable, last resort)
    """

    def __init__(self, config: AgentBrowserConfig | None = None) -> None:
        self.config = config or AgentBrowserConfig()
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._chrome_process: subprocess.Popen | None = None
        self._owns_browser = False  # True if WE launched Chrome (so we should close it)
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return

        self._pw = await async_playwright().start()

        # ── Mode 1: CDP connect to existing Chrome ──
        if self.config.cdp_url:
            logger.info(f"Connecting to existing Chrome at {self.config.cdp_url}")
            self._browser = await self._pw.chromium.connect_over_cdp(self.config.cdp_url)
            self._owns_browser = False
            # Use existing contexts/pages
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
                pages = self._context.pages
                self._page = pages[0] if pages else await self._context.new_page()
            else:
                self._context = await self._browser.new_context()
                self._page = await self._context.new_page()
        else:
            # ── Mode 2: Auto-launch Chrome with remote debugging ──
            cdp_url = await self._auto_launch_chrome()
            if cdp_url:
                logger.info(f"Connecting to auto-launched Chrome at {cdp_url}")
                self._browser = await self._pw.chromium.connect_over_cdp(cdp_url)
                self._owns_browser = True
                contexts = self._browser.contexts
                if contexts:
                    self._context = contexts[0]
                    pages = self._context.pages
                    self._page = pages[0] if pages else await self._context.new_page()
                else:
                    self._context = await self._browser.new_context()
                    self._page = await self._context.new_page()
            else:
                # ── Mode 3: Fallback — Playwright launch ──
                logger.warning("CDP unavailable, falling back to Playwright launch (may be detected)")
                self._browser = await self._pw.chromium.launch(
                    headless=self.config.headless,
                    channel="chrome",
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
                self._owns_browser = True
                ctx_opts: dict[str, Any] = {
                    "viewport": {"width": self.config.viewport_width, "height": self.config.viewport_height},
                    "locale": self.config.locale,
                    "timezone_id": self.config.timezone_id,
                }
                if self.config.stealth_mode:
                    ctx_opts["extra_http_headers"] = STEALTH_HEADERS
                    ctx_opts["bypass_csp"] = True
                self._context = await self._browser.new_context(**ctx_opts)
                if self.config.stealth_mode:
                    await self._context.add_init_script(STEALTH_JS)
                self._page = await self._context.new_page()

        self._page.set_default_timeout(self.config.default_timeout)
        self._initialized = True
        logger.info(f"AgentBrowser initialized (owns_browser={self._owns_browser})")

    async def _auto_launch_chrome(self) -> str | None:
        """Launch Chrome with --remote-debugging-port and return the CDP URL."""
        import tempfile

        chrome_path = self._find_chrome()
        if not chrome_path:
            logger.warning("Chrome not found on system")
            return None

        port = self.config.auto_launch_debug_port or self._find_free_port()

        # Always use a separate user-data-dir so we don't conflict with
        # the user's running Chrome instance
        user_data = self.config.user_data_dir
        if not user_data:
            user_data = tempfile.mkdtemp(prefix="predator_chrome_")
            logger.info(f"Using temp Chrome profile: {user_data}")

        args = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-client-side-phishing-detection",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-hang-monitor",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--disable-sync",
            "--disable-translate",
            "--metrics-recording-only",
            "--no-service-autorun",
            "--password-store=basic",
            f"--window-size={self.config.viewport_width},{self.config.viewport_height}",
        ]

        if self.config.headless:
            args.append("--headless=new")

        logger.info(f"Launching Chrome on port {port}: {chrome_path}")
        self._chrome_process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Wait for Chrome to start listening (up to 10 seconds)
        cdp_url = f"http://localhost:{port}"
        for i in range(100):
            await asyncio.sleep(0.1)
            # Check if process died
            if self._chrome_process.poll() is not None:
                stderr = self._chrome_process.stderr.read().decode() if self._chrome_process.stderr else ""
                logger.error(f"Chrome exited with code {self._chrome_process.returncode}: {stderr[:500]}")
                return None
            try:
                conn = socket.create_connection(("localhost", port), timeout=0.5)
                conn.close()
                logger.info(f"Chrome ready at {cdp_url} (took {(i+1)*100}ms)")
                return cdp_url
            except (ConnectionRefusedError, OSError):
                continue

        logger.error(f"Chrome failed to start on port {port} after 10s")
        return None

    @staticmethod
    def _find_chrome() -> str | None:
        """Find Chrome executable on the system."""
        candidates = [
            # macOS
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            # Linux
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
            shutil.which("chromium-browser"),
            shutil.which("chromium"),
            # Windows
            shutil.which("chrome"),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return None

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]

    async def close(self) -> None:
        try:
            if self._owns_browser:
                if self._context:
                    await self._context.close()
                if self._browser:
                    await self._browser.close()
            else:
                # CDP mode: we don't own the browser, just disconnect
                if self._browser:
                    await self._browser.close()  # disconnects CDP, doesn't kill Chrome
        except Exception:
            pass
        if self._pw:
            await self._pw.stop()
        if self._chrome_process:
            self._chrome_process.terminate()
            self._chrome_process = None
        self._initialized = False
        logger.info("AgentBrowser closed")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator["AgentBrowser", None]:
        await self.initialize()
        try:
            yield self
        finally:
            await self.close()

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Browser not initialized")
        return self._page

    # ─── Core primitives ─────────────────────

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        """Navigate to a URL. Handles WAF/bot challenges automatically."""
        try:
            await self.page.goto(url, wait_until=wait_until)
            # Wait for JS-heavy sites to finish rendering
            try:
                await self.page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(1)

            # Detect WAF challenge pages (AWS WAF, Cloudflare, etc.)
            # These pages have JS that auto-resolves by refreshing the page
            for attempt in range(12):  # Wait up to ~30 seconds
                page_content = await self.page.content()
                is_waf = any(kw in page_content for kw in [
                    "AwsWafIntegration", "challenge-platform",
                    "cf-browser-verification", "Checking your browser",
                    "Just a moment", "_cf_chl_opt",
                ])
                if not is_waf:
                    break
                logger.info(f"WAF challenge detected, waiting... (attempt {attempt + 1}/12)")
                await asyncio.sleep(2.5)
                # Check if page navigated (WAF auto-refresh)
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeout:
                    pass

            return {
                "success": True,
                "url": self.page.url,
                "title": await self.page.title(),
            }
        except (PlaywrightTimeout, PlaywrightError) as e:
            return {"success": False, "error": str(e)}

    async def screenshot(self, full_page: bool = False) -> str:
        """Take screenshot, returns base64-encoded PNG."""
        png = await self.page.screenshot(full_page=full_page, type="png")
        return base64.b64encode(png).to_bytes_string() if hasattr(png, 'to_bytes_string') else base64.b64encode(png).decode("ascii")

    async def get_state(self) -> dict[str, Any]:
        """
        Get full page state for LLM reasoning.
        Returns: url, title, interactive elements with bounding boxes,
        scroll position, and condensed visible text.
        """
        elements = await self.page.evaluate(EXTRACT_ELEMENTS_JS)
        scroll = await self.page.evaluate(GET_SCROLL_INFO_JS)
        text = await self.page.evaluate(GET_PAGE_TEXT_JS, 3000)

        return {
            "url": self.page.url,
            "title": await self.page.title(),
            "viewport": {"width": self.config.viewport_width, "height": self.config.viewport_height},
            "scroll": scroll,
            "elements": elements,
            "element_count": len(elements),
            "page_text": text,
        }

    async def click(
        self,
        *,
        element_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
        selector: str | None = None,
    ) -> dict[str, Any]:
        """
        Click an element. Provide ONE of:
          - element_id: ID from get_state() elements list
          - x, y: absolute page coordinates
          - selector: CSS selector (fallback)
        """
        try:
            if element_id is not None:
                elem = await self._resolve_element(element_id)
                if not elem:
                    return {"success": False, "error": f"Element {element_id} not found"}
                cx = elem["bbox"]["x"] + elem["bbox"]["w"] // 2
                cy = elem["bbox"]["y"] + elem["bbox"]["h"] // 2
                await self.page.mouse.click(cx, cy)
                return {"success": True, "clicked": elem.get("name", ""), "at": {"x": cx, "y": cy}}

            elif x is not None and y is not None:
                await self.page.mouse.click(x, y)
                return {"success": True, "at": {"x": x, "y": y}}

            elif selector:
                await self.page.click(selector, timeout=10000)
                return {"success": True, "selector": selector}

            else:
                return {"success": False, "error": "Provide element_id, (x,y), or selector"}

        except (PlaywrightTimeout, PlaywrightError) as e:
            return {"success": False, "error": str(e)}

    async def type_text(
        self,
        text: str,
        *,
        element_id: int | None = None,
        selector: str | None = None,
        press_enter: bool = False,
        clear_first: bool = True,
    ) -> dict[str, Any]:
        """
        Type text into an input. If element_id or selector given, clicks it first.
        If neither, types into the currently focused element.
        """
        try:
            if element_id is not None:
                elem = await self._resolve_element(element_id)
                if not elem:
                    return {"success": False, "error": f"Element {element_id} not found"}
                cx = elem["bbox"]["x"] + elem["bbox"]["w"] // 2
                cy = elem["bbox"]["y"] + elem["bbox"]["h"] // 2
                await self.page.mouse.click(cx, cy)
                await asyncio.sleep(0.1)
            elif selector:
                await self.page.click(selector, timeout=5000)
                await asyncio.sleep(0.1)

            if clear_first:
                await self.page.keyboard.press("Meta+a")
                await asyncio.sleep(0.05)

            await self.page.keyboard.type(text, delay=50)

            if press_enter:
                await asyncio.sleep(0.1)
                await self.page.keyboard.press("Enter")

            return {"success": True, "typed": text}

        except (PlaywrightTimeout, PlaywrightError) as e:
            return {"success": False, "error": str(e)}

    async def scroll(self, direction: str = "down", amount: int = 3) -> dict[str, Any]:
        """Scroll the page. direction: up/down/left/right. amount: number of 'ticks'."""
        delta_map = {
            "down": (0, 300 * amount),
            "up": (0, -300 * amount),
            "right": (300 * amount, 0),
            "left": (-300 * amount, 0),
        }
        dx, dy = delta_map.get(direction, (0, 300 * amount))
        await self.page.mouse.wheel(dx, dy)
        await asyncio.sleep(0.3)
        scroll = await self.page.evaluate(GET_SCROLL_INFO_JS)
        return {"success": True, "scroll": scroll}

    async def press_key(self, key: str) -> dict[str, Any]:
        """Press a keyboard key (Enter, Tab, Escape, Backspace, ArrowDown, etc.)."""
        try:
            await self.page.keyboard.press(key)
            return {"success": True, "key": key}
        except PlaywrightError as e:
            return {"success": False, "error": str(e)}

    async def select_option(
        self,
        value: str,
        *,
        element_id: int | None = None,
        selector: str | None = None,
    ) -> dict[str, Any]:
        """Select an option from a <select> dropdown."""
        try:
            if element_id is not None:
                elements = await self.page.evaluate(EXTRACT_ELEMENTS_JS)
                elem = next((e for e in elements if e["id"] == element_id), None)
                if not elem:
                    return {"success": False, "error": f"Element {element_id} not found"}
                # Build a selector from the element
                if elem.get("tag") == "select":
                    sel = f'select#{elem.get("name", "")}' if elem.get("name") else f'select'
                    # Try by bbox click then select
                    cx = elem["bbox"]["x"] + elem["bbox"]["w"] // 2
                    cy = elem["bbox"]["y"] + elem["bbox"]["h"] // 2
                    locator = self.page.locator(f"select >> nth=0").first
                    # Better: use evaluate to find the element
                    await self.page.evaluate(
                        f"""(val) => {{
                            const els = document.querySelectorAll('select');
                            for (const el of els) {{
                                const r = el.getBoundingClientRect();
                                if (Math.abs(r.x - {elem['bbox']['x']}) < 5 && Math.abs(r.y - {elem['bbox']['y']}) < 5) {{
                                    el.value = val;
                                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                                    return;
                                }}
                            }}
                        }}""",
                        value,
                    )
                    return {"success": True, "selected": value}
            elif selector:
                await self.page.select_option(selector, value)
                return {"success": True, "selected": value, "selector": selector}
            return {"success": False, "error": "Provide element_id or selector"}
        except (PlaywrightTimeout, PlaywrightError) as e:
            return {"success": False, "error": str(e)}

    async def hover(self, *, element_id: int | None = None, x: int | None = None, y: int | None = None) -> dict[str, Any]:
        """Hover over an element or coordinate."""
        try:
            if element_id is not None:
                elem = await self._resolve_element(element_id)
                if not elem:
                    return {"success": False, "error": f"Element {element_id} not found"}
                cx = elem["bbox"]["x"] + elem["bbox"]["w"] // 2
                cy = elem["bbox"]["y"] + elem["bbox"]["h"] // 2
                await self.page.mouse.move(cx, cy)
                return {"success": True, "at": {"x": cx, "y": cy}}
            elif x is not None and y is not None:
                await self.page.mouse.move(x, y)
                return {"success": True, "at": {"x": x, "y": y}}
            return {"success": False, "error": "Provide element_id or (x,y)"}
        except PlaywrightError as e:
            return {"success": False, "error": str(e)}

    async def go_back(self) -> dict[str, Any]:
        try:
            await self.page.go_back(wait_until="domcontentloaded")
            return {"success": True, "url": self.page.url}
        except (PlaywrightTimeout, PlaywrightError) as e:
            return {"success": False, "error": str(e)}

    async def go_forward(self) -> dict[str, Any]:
        try:
            await self.page.go_forward(wait_until="domcontentloaded")
            return {"success": True, "url": self.page.url}
        except (PlaywrightTimeout, PlaywrightError) as e:
            return {"success": False, "error": str(e)}

    async def wait(self, ms: int = 1000, selector: str | None = None) -> dict[str, Any]:
        """Wait for a duration (ms) or for a selector to appear."""
        try:
            if selector:
                await self.page.wait_for_selector(selector, timeout=ms or 10000)
                return {"success": True, "found": selector}
            else:
                await asyncio.sleep(ms / 1000)
                return {"success": True, "waited_ms": ms}
        except PlaywrightTimeout:
            return {"success": False, "error": f"Timeout waiting for {selector}"}

    async def get_text(self, max_length: int = 5000) -> dict[str, Any]:
        """Get visible text content of the page."""
        text = await self.page.evaluate(GET_PAGE_TEXT_JS, max_length)
        return {"text": text, "length": len(text)}

    # ─── Tab management ──────────────────────

    async def new_tab(self, url: str | None = None) -> dict[str, Any]:
        """Open a new tab, optionally navigating to a URL."""
        if not self._context:
            return {"success": False, "error": "No browser context"}
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.config.default_timeout)
        if url:
            await self._page.goto(url, wait_until="domcontentloaded")
        return {"success": True, "tab_count": len(self._context.pages), "url": self._page.url}

    async def get_tabs(self) -> dict[str, Any]:
        """List all open tabs."""
        if not self._context:
            return {"tabs": []}
        tabs = []
        for i, p in enumerate(self._context.pages):
            tabs.append({"index": i, "url": p.url, "active": p == self._page})
        return {"tabs": tabs}

    async def switch_tab(self, index: int) -> dict[str, Any]:
        """Switch to tab by index."""
        if not self._context:
            return {"success": False, "error": "No browser context"}
        pages = self._context.pages
        if 0 <= index < len(pages):
            self._page = pages[index]
            await self._page.bring_to_front()
            return {"success": True, "url": self._page.url}
        return {"success": False, "error": f"Tab index {index} out of range (0-{len(pages)-1})"}

    async def close_tab(self, index: int | None = None) -> dict[str, Any]:
        """Close a tab. Defaults to current tab."""
        if not self._context:
            return {"success": False, "error": "No browser context"}
        pages = self._context.pages
        if len(pages) <= 1:
            return {"success": False, "error": "Cannot close the last tab"}
        target = pages[index] if index is not None and 0 <= index < len(pages) else self._page
        await target.close()
        self._page = self._context.pages[-1]
        return {"success": True, "remaining_tabs": len(self._context.pages)}

    # ─── Internal ────────────────────────────

    async def _resolve_element(self, element_id: int) -> dict[str, Any] | None:
        """Re-extract elements and find by ID."""
        elements = await self.page.evaluate(EXTRACT_ELEMENTS_JS)
        return next((e for e in elements if e["id"] == element_id), None)
