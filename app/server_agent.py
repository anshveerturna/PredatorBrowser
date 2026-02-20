"""
Predator Agent MCP Server — Agentic browser for LLM control.

Exposes simple primitives over MCP. The calling LLM is the brain.
Start with: python -m app.server_agent
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent

from app.core.agent_browser import AgentBrowser, AgentBrowserConfig

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("predator.server.agent")

_browser: AgentBrowser | None = None


def _cfg() -> AgentBrowserConfig:
    return AgentBrowserConfig(
        cdp_url=os.getenv("PREDATOR_CDP_URL"),  # e.g. "http://localhost:9222"
        user_data_dir=os.getenv("PREDATOR_USER_DATA_DIR"),
        headless=os.getenv("PREDATOR_HEADLESS", "false").lower() == "true",
        viewport_width=int(os.getenv("PREDATOR_VIEWPORT_WIDTH", "1440")),
        viewport_height=int(os.getenv("PREDATOR_VIEWPORT_HEIGHT", "900")),
        stealth_mode=os.getenv("PREDATOR_STEALTH", "true").lower() == "true",
    )


async def get_browser() -> AgentBrowser:
    global _browser
    if _browser is None:
        _browser = AgentBrowser(_cfg())
        await _browser.initialize()
    return _browser


async def cleanup() -> None:
    global _browser
    if _browser:
        await _browser.close()
        _browser = None


server = Server("predator-agent")


def _text(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _image_and_text(b64: str, info: dict) -> list[TextContent | ImageContent]:
    return [
        ImageContent(type="image", data=b64, mimeType="image/png"),
        TextContent(type="text", text=json.dumps(info, default=str)),
    ]


# ─── Tool Definitions ───────────────────────

TOOLS = [
    Tool(
        name="navigate",
        description="Navigate to a URL. Returns page info (url, title).",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="screenshot",
        description="Take a screenshot of the current page. Returns a PNG image of what the browser currently shows. Use this to SEE the page and understand what is on screen.",
        inputSchema={
            "type": "object",
            "properties": {
                "full_page": {"type": "boolean", "description": "Capture the full scrollable page (default: false, viewport only)", "default": False},
            },
        },
    ),
    Tool(
        name="get_state",
        description="Get the full state of the page: URL, title, all interactive elements (buttons, links, inputs, selects) with their bounding boxes, scroll position, and visible text. Use this to understand what you can interact with.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="click",
        description="Click an element on the page. Provide ONE of: element_id (from get_state), coordinates (x, y), or a CSS selector.",
        inputSchema={
            "type": "object",
            "properties": {
                "element_id": {"type": "integer", "description": "Element ID from get_state() elements list"},
                "x": {"type": "integer", "description": "X coordinate to click"},
                "y": {"type": "integer", "description": "Y coordinate to click"},
                "selector": {"type": "string", "description": "CSS selector (fallback)"},
            },
        },
    ),
    Tool(
        name="type_text",
        description="Type text into an input field. Optionally specify which element to type into via element_id or selector. If neither is given, types into the currently focused element.",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
                "element_id": {"type": "integer", "description": "Element ID to click first before typing"},
                "selector": {"type": "string", "description": "CSS selector to click first before typing"},
                "press_enter": {"type": "boolean", "description": "Press Enter after typing (default: false)", "default": False},
                "clear_first": {"type": "boolean", "description": "Clear the field before typing (default: true)", "default": True},
            },
            "required": ["text"],
        },
    ),
    Tool(
        name="scroll",
        description="Scroll the page. Returns new scroll position.",
        inputSchema={
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "Scroll direction", "default": "down"},
                "amount": {"type": "integer", "description": "Number of scroll ticks (default: 3)", "default": 3},
            },
        },
    ),
    Tool(
        name="press_key",
        description="Press a keyboard key. Examples: Enter, Tab, Escape, Backspace, ArrowDown, ArrowUp, Space, Meta+a, Control+c.",
        inputSchema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key to press"},
            },
            "required": ["key"],
        },
    ),
    Tool(
        name="select_option",
        description="Select an option from a <select> dropdown element.",
        inputSchema={
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "Option value to select"},
                "element_id": {"type": "integer", "description": "Element ID of the select"},
                "selector": {"type": "string", "description": "CSS selector of the select"},
            },
            "required": ["value"],
        },
    ),
    Tool(
        name="hover",
        description="Hover over an element or coordinate.",
        inputSchema={
            "type": "object",
            "properties": {
                "element_id": {"type": "integer", "description": "Element ID to hover"},
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
            },
        },
    ),
    Tool(
        name="go_back",
        description="Navigate back in browser history.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="go_forward",
        description="Navigate forward in browser history.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="wait",
        description="Wait for a duration or for a CSS selector to appear.",
        inputSchema={
            "type": "object",
            "properties": {
                "ms": {"type": "integer", "description": "Milliseconds to wait", "default": 1000},
                "selector": {"type": "string", "description": "CSS selector to wait for"},
            },
        },
    ),
    Tool(
        name="get_text",
        description="Get visible text content from the page.",
        inputSchema={
            "type": "object",
            "properties": {
                "max_length": {"type": "integer", "description": "Maximum text length to return", "default": 5000},
            },
        },
    ),
    Tool(
        name="new_tab",
        description="Open a new browser tab, optionally navigating to a URL.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open in new tab"},
            },
        },
    ),
    Tool(
        name="get_tabs",
        description="List all open browser tabs.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="switch_tab",
        description="Switch to a specific browser tab by index.",
        inputSchema={
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Tab index to switch to"},
            },
            "required": ["index"],
        },
    ),
    Tool(
        name="close_tab",
        description="Close a browser tab.",
        inputSchema={
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Tab index to close (default: current tab)"},
            },
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
    b = await get_browser()

    try:
        if name == "navigate":
            return _text(await b.navigate(arguments["url"]))

        elif name == "screenshot":
            b64 = await b.screenshot(full_page=arguments.get("full_page", False))
            return _image_and_text(b64, {"url": b.page.url, "title": await b.page.title()})

        elif name == "get_state":
            return _text(await b.get_state())

        elif name == "click":
            return _text(await b.click(
                element_id=arguments.get("element_id"),
                x=arguments.get("x"),
                y=arguments.get("y"),
                selector=arguments.get("selector"),
            ))

        elif name == "type_text":
            return _text(await b.type_text(
                arguments["text"],
                element_id=arguments.get("element_id"),
                selector=arguments.get("selector"),
                press_enter=arguments.get("press_enter", False),
                clear_first=arguments.get("clear_first", True),
            ))

        elif name == "scroll":
            return _text(await b.scroll(
                direction=arguments.get("direction", "down"),
                amount=arguments.get("amount", 3),
            ))

        elif name == "press_key":
            return _text(await b.press_key(arguments["key"]))

        elif name == "select_option":
            return _text(await b.select_option(
                arguments["value"],
                element_id=arguments.get("element_id"),
                selector=arguments.get("selector"),
            ))

        elif name == "hover":
            return _text(await b.hover(
                element_id=arguments.get("element_id"),
                x=arguments.get("x"),
                y=arguments.get("y"),
            ))

        elif name == "go_back":
            return _text(await b.go_back())

        elif name == "go_forward":
            return _text(await b.go_forward())

        elif name == "wait":
            return _text(await b.wait(
                ms=arguments.get("ms", 1000),
                selector=arguments.get("selector"),
            ))

        elif name == "get_text":
            return _text(await b.get_text(max_length=arguments.get("max_length", 5000)))

        elif name == "new_tab":
            return _text(await b.new_tab(url=arguments.get("url")))

        elif name == "get_tabs":
            return _text(await b.get_tabs())

        elif name == "switch_tab":
            return _text(await b.switch_tab(arguments["index"]))

        elif name == "close_tab":
            return _text(await b.close_tab(index=arguments.get("index")))

        else:
            return _text({"error": f"Unknown tool: {name}"})

    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return _text({"error": str(e)})


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Predator Agent MCP Server starting...")
        try:
            await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            await cleanup()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
