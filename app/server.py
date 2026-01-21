"""
Predator Browser - MCP Server Entry Point

This module exposes the PredatorBrowser as a Model Context Protocol (MCP) server,
allowing LLM agents to interact with web pages using the Waterfall Cost-Logic.

Tools exposed:
- browse: Navigate to URL and execute goal
- click: Smart click using Level 2/3
- type: Fill text into input fields
- extract_data: Extract structured data from page
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    ImageContent,
    EmbeddedResource
)

from app.core.predator import PredatorBrowser, BrowserConfig, ExecutionResult

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger("predator.server")

# Global browser instance
_browser: PredatorBrowser | None = None


def get_browser_config() -> BrowserConfig:
    """Get browser configuration from environment variables."""
    return BrowserConfig(
        headless=os.getenv("PREDATOR_HEADLESS", "true").lower() == "true",
        viewport_width=int(os.getenv("PREDATOR_VIEWPORT_WIDTH", "1920")),
        viewport_height=int(os.getenv("PREDATOR_VIEWPORT_HEIGHT", "1080")),
        user_agent=os.getenv("PREDATOR_USER_AGENT"),
        stealth_mode=os.getenv("PREDATOR_STEALTH", "true").lower() == "true",
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        router_model=os.getenv("PREDATOR_ROUTER_MODEL", "gpt-4o-mini"),
        vision_model=os.getenv("PREDATOR_VISION_MODEL", "gpt-4o")
    )


async def get_browser() -> PredatorBrowser:
    """Get or create the global browser instance."""
    global _browser
    
    if _browser is None:
        config = get_browser_config()
        _browser = PredatorBrowser(config)
        await _browser.initialize()
        logger.info("[Server] Browser initialized")
    
    return _browser


async def cleanup_browser() -> None:
    """Cleanup the global browser instance."""
    global _browser
    
    if _browser is not None:
        await _browser.close()
        _browser = None
        logger.info("[Server] Browser closed")


def result_to_content(result: ExecutionResult) -> list[TextContent | ImageContent]:
    """Convert ExecutionResult to MCP content."""
    content: list[TextContent | ImageContent] = []
    
    # Main result as JSON
    content.append(TextContent(
        type="text",
        text=json.dumps(result.to_dict(), indent=2, default=str)
    ))
    
    return content


# Create MCP Server
server = Server("predator-browser")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="browse",
            description="""Navigate to a URL and execute a goal using the Waterfall Cost-Logic.
            
The Predator Browser follows a strict hierarchy:
1. Level 1 (Shadow API): Intercepts network traffic for data (0 cost, max speed)
2. Level 2 (Blind Map): Uses Accessibility Tree for interactions (Low cost)
3. Level 3 (Eagle Eye): Uses Vision with Set-of-Marks as last resort (High cost)

Use this for:
- Navigating to websites
- Finding specific information on a page
- Clicking buttons or links with a goal
- Any interaction that requires intelligent routing""",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to navigate to"
                    },
                    "goal": {
                        "type": "string",
                        "description": "The goal to achieve (e.g., 'Find the price of the flight', 'Click the login button')"
                    }
                },
                "required": ["url", "goal"]
            }
        ),
        Tool(
            name="click",
            description="""Smart click on an element using Level 2 (AX Tree) first, then Level 3 (Vision) if needed.

Includes a validator that checks if URL changed or DOM updated after click.

Use this for:
- Clicking buttons, links, or interactive elements
- Navigating menus
- Submitting forms""",
            inputSchema={
                "type": "object",
                "properties": {
                    "element_description": {
                        "type": "string",
                        "description": "Description of the element to click (e.g., 'Submit button', 'Login link', 'Search icon')"
                    }
                },
                "required": ["element_description"]
            }
        ),
        Tool(
            name="type",
            description="""Type text into an input field using AX Tree navigation.

Locates input fields by their description (placeholder, label, or accessible name).

Use this for:
- Filling form fields
- Entering search queries
- Typing in text areas""",
            inputSchema={
                "type": "object",
                "properties": {
                    "field_description": {
                        "type": "string",
                        "description": "Description of the input field (e.g., 'Email address', 'Search box', 'Password field')"
                    },
                    "text": {
                        "type": "string",
                        "description": "The text to type into the field"
                    }
                },
                "required": ["field_description", "text"]
            }
        ),
        Tool(
            name="extract_data",
            description="""Extract structured data from the page.

Prioritizes Shadow API (Level 1) to fill the schema from network traffic,
then falls back to AX Tree (Level 2), then Vision (Level 3).

Use this for:
- Scraping specific data points
- Extracting product information
- Getting structured content from pages""",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {
                        "type": "object",
                        "description": "Expected data schema with field names and types (e.g., {\"title\": \"string\", \"price\": \"number\", \"description\": \"string\"})",
                        "additionalProperties": {
                            "type": "string"
                        }
                    }
                },
                "required": ["schema"]
            }
        ),
        Tool(
            name="screenshot",
            description="""Take a screenshot of the current page.

Can capture either the visible viewport or the full scrollable page.

Use this for:
- Debugging page state
- Visual verification
- Capturing evidence""",
            inputSchema={
                "type": "object",
                "properties": {
                    "full_page": {
                        "type": "boolean",
                        "description": "Whether to capture the full scrollable page (default: false)",
                        "default": False
                    },
                    "marked": {
                        "type": "boolean",
                        "description": "Whether to apply Set-of-Marks annotations (default: false)",
                        "default": False
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="page_info",
            description="""Get information about the current page.

Returns URL, title, and viewport size.

Use this for:
- Checking current location
- Verifying page state
- Getting context""",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="navigate",
            description="""Navigate to a URL without executing a goal.

Use this for simple navigation when you just need to load a page.

For intelligent interactions, use the 'browse' tool instead.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to navigate to"
                    },
                    "wait_until": {
                        "type": "string",
                        "description": "Wait condition: 'load', 'domcontentloaded', or 'networkidle'",
                        "default": "domcontentloaded",
                        "enum": ["load", "domcontentloaded", "networkidle"]
                    }
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="get_ax_tree",
            description="""Get the Accessibility Tree of the current page.

Returns a condensed markdown representation of interactive elements.

Use this for:
- Understanding page structure
- Debugging element selection
- Manual element identification""",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_nodes": {
                        "type": "integer",
                        "description": "Maximum number of nodes to return (default: 200)",
                        "default": 200
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_network_log",
            description="""Get the captured network traffic log.

Returns a summary of JSON API responses captured by Level 1 (Shadow API).

Use this for:
- Debugging data extraction
- Understanding API structure
- Finding hidden data sources""",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
    """Handle tool calls."""
    logger.info(f"[Server] Tool called: {name} with args: {arguments}")
    
    try:
        browser = await get_browser()
        
        if name == "browse":
            url = arguments["url"]
            goal = arguments["goal"]
            
            # Navigate first
            nav_success = await browser.navigate(url)
            if not nav_success:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"Failed to navigate to {url}"
                    })
                )]
            
            # Execute goal
            result = await browser.execute_goal(goal)
            return result_to_content(result)
        
        elif name == "click":
            element_description = arguments["element_description"]
            result = await browser.click(element_description)
            return result_to_content(result)
        
        elif name == "type":
            field_description = arguments["field_description"]
            text = arguments["text"]
            result = await browser.type_text(field_description, text)
            return result_to_content(result)
        
        elif name == "extract_data":
            schema = arguments["schema"]
            result = await browser.extract_data(schema)
            return result_to_content(result)
        
        elif name == "screenshot":
            full_page = arguments.get("full_page", False)
            marked = arguments.get("marked", False)
            
            if marked:
                image_base64 = await browser.get_marked_screenshot()
            else:
                image_bytes = await browser.screenshot(full_page=full_page)
                image_base64 = base64.b64encode(image_bytes).decode("utf-8")
            
            return [ImageContent(
                type="image",
                data=image_base64,
                mimeType="image/png"
            )]
        
        elif name == "page_info":
            info = await browser.get_page_info()
            return [TextContent(
                type="text",
                text=json.dumps(info, indent=2)
            )]
        
        elif name == "navigate":
            url = arguments["url"]
            wait_until = arguments.get("wait_until", "domcontentloaded")
            
            success = await browser.navigate(url, wait_until=wait_until)
            info = await browser.get_page_info()
            
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": success,
                    "page": info
                }, indent=2)
            )]
        
        elif name == "get_ax_tree":
            max_nodes = arguments.get("max_nodes", 200)
            
            if browser.navigator:
                tree = await browser.navigator.get_condensed_tree(max_nodes=max_nodes)
                return [TextContent(
                    type="text",
                    text=tree
                )]
            else:
                return [TextContent(
                    type="text",
                    text="Navigator not initialized"
                )]
        
        elif name == "get_network_log":
            if browser.sniffer:
                summary = browser.sniffer.get_buffer_summary()
                return [TextContent(
                    type="text",
                    text=json.dumps(summary, indent=2, default=str)
                )]
            else:
                return [TextContent(
                    type="text",
                    text="Sniffer not initialized"
                )]
        
        else:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"})
            )]
    
    except Exception as e:
        logger.exception(f"[Server] Error in tool {name}: {e}")
        return [TextContent(
            type="text",
            text=json.dumps({
                "error": str(e),
                "tool": name
            })
        )]


async def main() -> None:
    """Main entry point for the MCP server."""
    logger.info("[Server] Starting Predator Browser MCP Server...")
    
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    finally:
        await cleanup_browser()


def run() -> None:
    """Synchronous entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
