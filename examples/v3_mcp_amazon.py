"""
Demonstration of Predator v3 Hybrid UI Intelligence over MCP.

This script acts as an MCP client. It starts the Predator MCP server,
fetches the structured state over the MCP protocol, and uses the newly
built v3 IntentRanker locally to find the correct elements to interact with.

Flow:
1. Navigate to Amazon
2. Rank elements for "Search box" intent -> Type search
3. Rank elements for "Optimum Nutrition Gold Standard 2 lbs Double Rich Chocolate" intent -> Click product
4. Rank elements for "Add to cart" intent -> Click add
"""

import asyncio
import json
import logging
import os
from pprint import pprint

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Import v3 intelligence layer
from app.core.v3.intent_ranker import IntentRanker
from app.core.v2.contracts import ActionType
from app.core.v2.state_models import InteractiveElementState

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("v3_mcp_amazon")

ARTIFACT_DIR = "/Users/anshveerturna/.gemini/antigravity/brain/f9809600-d6a6-4397-b93b-bd4e3e093802"

async def save_screenshot(session: ClientSession, name: str):
    """Call MCP screenshot tool and save to artifact dir."""
    logger.info(f"📸 Taking screenshot: {name}")
    result = await session.call_tool("screenshot", {})
    if result.content and len(result.content) > 0:
        img_content = result.content[0]
        if hasattr(img_content, 'data') and img_content.data:
            import base64
            img_data = base64.b64decode(img_content.data)
            path = os.path.join(ARTIFACT_DIR, f"{name}.png")
            with open(path, "wb") as f:
                f.write(img_data)
            logger.info(f"Saved {path}")


def parse_mcp_state(result_content: list) -> list[InteractiveElementState]:
    """Parse the MCP get_state unstructured JSON into InteractiveElementState objects."""
    state_json = json.loads(result_content[0].text)
    elements_data = state_json.get("elements", [])
    
    vw = state_json.get("viewport", {}).get("width", 1440)
    vh = state_json.get("viewport", {}).get("height", 900)
    
    elements = []
    for ed in elements_data:
        bx = ed.get("bbox", {}).get("x", 0)
        by = ed.get("bbox", {}).get("y", 0)
        bw = ed.get("bbox", {}).get("w", 0)
        bh = ed.get("bbox", {}).get("h", 0)
        bbox_norm = (bx/vw, by/vh, bw/vw, bh/vh) if vw and vh else (0,0,0,0)

        # Skip elements that have literally no semantic info
        name = ed.get("name", "").strip()
        role = ed.get("role") or ed.get("tag", "")
        if not name and not role:
            continue

        el = InteractiveElementState(
            eid=str(ed.get("id", "")),
            fid="f_main",
            role=role,
            name_short=name,
            element_type=ed.get("tag", ""),
            enabled=ed.get("enabled", True),
            visible=True,
            required=False,
            checked=ed.get("checked"),
            value_hint=None,
            bbox_norm=bbox_norm,
            selector_hint_id=None,
            stability_score=0.5,
            selector_hints=(),
        )
        elements.append(el)
    return elements


async def main():
    logger.info("Starting Predator MCP Server...")
    
    # Path to the MCP server script
    server_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app", "server_agent.py"))
    
    server_params = StdioServerParameters(
        command="python3",
        args=["-m", "app.server_agent"],
        env=os.environ.copy()
    )

    ranker = IntentRanker()
    logger.info(f"Initialized IntentRanker version {ranker.version}")

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            logger.info("Connected to MCP server.")

            # 1. Navigate to Amazon
            logger.info("Navigating to Amazon.in...")
            await session.call_tool("navigate", {"url": "https://www.amazon.in/"})
            await asyncio.sleep(3) # Wait for page load
            await save_screenshot(session, "v3_mcp_1_home")

            # 2. Search for product using IntentRanker to find search box
            logger.info("Fetching state...")
            res = await session.call_tool("get_state", {})
            elements = parse_mcp_state(res.content)
            logger.info(f"Extracted {len(elements)} elements.")
            
            search_intent = "Search input box twotabsearchtextbox"
            logger.info(f"Ranking elements for intent: '{search_intent}'")
            ranked = ranker.rank(elements, search_intent, ActionType.TYPE)
            
            if not ranked:
                logger.error("No elements to rank!")
                return
                
            best_match = ranked[0]
            logger.info(f"Best match for search: eid={best_match.eid}, score={best_match.score:.3f}, signals={best_match.match_signals}")
            
            logger.info("Typing search query...")
            await session.call_tool("type_text", {
                "element_id": int(best_match.eid),
                "text": "Optimum Nutrition Whey Protein Powder",
                "press_enter": True
            })
            await asyncio.sleep(4)
            await save_screenshot(session, "v3_mcp_2_search_results")

            # 3. Find and click the actual product, using scroll-to-find
            product_intent = "Optimum Nutrition (ON) Gold Standard 100% Whey Protein Powder 2 lbs Double Rich Chocolate"
            logger.info(f"Searching for exact product: '{product_intent}'...")
            
            found_product = False
            for scroll_attempt in range(5):
                res = await session.call_tool("get_state", {})
                elements = parse_mcp_state(res.content)
                ranked = ranker.rank(elements, product_intent, ActionType.CLICK)
                
                # Filter to ensure we don't click competitors
                valid_candidates = [
                    c for c in ranked 
                    if c.score > 0.45 
                    and any(kw in next((e.name_short.lower() for e in elements if e.eid == c.eid), "") 
                            for kw in ["optimum", " on "])
                ]
                
                if valid_candidates:
                    best_match = valid_candidates[0]
                    name = next((e.name_short for e in elements if e.eid == best_match.eid), "")
                    logger.info(f"Found product! score={best_match.score:.3f}, name={name[:60]}")
                    
                    logger.info("Clicking the product link...")
                    await session.call_tool("click", {"element_id": int(best_match.eid)})
                    await asyncio.sleep(4)
                    await save_screenshot(session, "v3_mcp_3_product_page")
                    found_product = True
                    break
                    
                logger.info(f"Attempt {scroll_attempt+1}: Product not found in view. Scrolling down...")
                await session.call_tool("scroll", {"direction": "down", "amount": 4})
                await asyncio.sleep(2)
                
            if not found_product:
                logger.error("Failed to find the product after scrolling.")
                return

            # 4. Add to cart
            res = await session.call_tool("get_state", {})
            elements = parse_mcp_state(res.content)
            
            cart_intent = "Add to Cart button submit"
            logger.info(f"Ranking {len(elements)} elements for intent: '{cart_intent}'")
            ranked = ranker.rank(elements, cart_intent, ActionType.CLICK)
            
            best_match = ranked[0]
            logger.info(f"Best match for cart: eid={best_match.eid}, score={best_match.score:.3f}, signals={best_match.match_signals}")
            
            logger.info("Clicking Add to Cart...")
            await session.call_tool("click", {"element_id": int(best_match.eid)})
            await asyncio.sleep(4)
            await save_screenshot(session, "v3_mcp_4_cart_success")
            
            logger.info("Amazon Add to Cart MCP flow complete!")

if __name__ == "__main__":
    asyncio.run(main())
