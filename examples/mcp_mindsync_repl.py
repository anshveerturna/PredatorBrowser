"""
Interactive MCP Client for Autonomous LLM Control.
This script connects to the Predator MCP Server and provides a REPL.
The LLM (Antigravity) interacts with this script via stdin/stdout to
act as the "Brain", demonstrating context-aware navigation and dynamic
pop-up handling.

v2 — Added: screenshot, key, tabs, switchtab, closetab, text commands
     for full visual awareness and popup handling.
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logging.basicConfig(level=logging.WARNING)

SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

def format_state(result_content: list) -> str:
    try:
        state = json.loads(result_content[0].text)
        elements = state.get("elements", [])
        overlay_detected = state.get("overlay_detected", False)
        overlay_reason = state.get("overlay_reason", "")
        
        lines = []
        lines.append(f"URL: {state.get('url')}")
        lines.append(f"TITLE: {state.get('title')}")
        
        if overlay_detected:
            lines.append("=" * 40)
            lines.append("⚠  OVERLAY/POPUP DETECTED")
            if overlay_reason:
                lines.append(f"   Source: {overlay_reason}")
            lines.append("=" * 40)
        
        lines.append("-" * 40)
        lines.append("INTERACTIVE ELEMENTS:")
        
        for el in elements:
            role = el.get("role") or el.get("tag", "")
            name = (el.get("name") or "").strip()
            eid = el.get("id")
            in_overlay = el.get("in_overlay", False)
            if not name and not role:
                continue
            prefix = "[OVERLAY] " if in_overlay else ""
            lines.append(f"  [{eid}] {prefix}{role}: {name[:80]}")
            
        lines.append("-" * 40)
        return "\n".join(lines)
    except Exception as e:
        return f"Error formatting state: {e}"

async def main():
    server_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app", "server_agent.py"))
    
    server_params = StdioServerParameters(
        command="python3",
        args=["-m", "app.server_agent"],
        env=os.environ.copy()
    )

    print("--- PREDATOR V3 MCP REPL ---", flush=True)
    print("Commands:", flush=True)
    print("  navigate <url>       — Go to URL", flush=True)
    print("  type <eid> <text>    — Type into element", flush=True)
    print("  click <eid>          — Click element by ID", flush=True)
    print("  scroll <dir>         — Scroll up/down/left/right", flush=True)
    print("  key <key>            — Press key (Escape, Enter, Tab, etc.)", flush=True)
    print("  screenshot [name]    — Take screenshot, save to file", flush=True)
    print("  text                 — Get visible page text", flush=True)
    print("  tabs                 — List open tabs", flush=True)
    print("  switchtab <index>    — Switch to tab by index", flush=True)
    print("  closetab [index]     — Close tab (default: current)", flush=True)
    print("  wait <ms>            — Wait milliseconds", flush=True)
    print("  quit                 — Exit REPL", flush=True)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Connected to MCP server.", flush=True)

            while True:
                # 1. Fetch and print state
                res = await session.call_tool("get_state", {})
                print("\nCURRENT PAGE STATE:", flush=True)
                print(format_state(res.content), flush=True)
                
                # 2. Wait for LLM command
                print(">> ", end="", flush=True)
                cmd_line = await asyncio.to_thread(sys.stdin.readline)
                if not cmd_line:
                    break
                    
                cmd_line = cmd_line.strip()
                if not cmd_line:
                    continue
                    
                parts = cmd_line.split(" ", 1)
                cmd = parts[0].lower()
                args = parts[1] if len(parts) > 1 else ""
                
                try:
                    if cmd == "quit" or cmd == "exit":
                        break

                    elif cmd == "navigate":
                        print(f"Executing: navigate to {args}", flush=True)
                        await session.call_tool("navigate", {"url": args})
                        await asyncio.sleep(2)

                    elif cmd == "click":
                        eid = int(args.strip())
                        print(f"Executing: click {eid}", flush=True)
                        await session.call_tool("click", {"element_id": eid})
                        await asyncio.sleep(2)

                    elif cmd == "type":
                        eid_str, text = args.split(" ", 1)
                        eid = int(eid_str)
                        print(f"Executing: type into {eid}: {text}", flush=True)
                        await session.call_tool("type_text", {
                            "element_id": eid,
                            "text": text,
                            "press_enter": True
                        })
                        await asyncio.sleep(3)

                    elif cmd == "scroll":
                        direction = args.strip() or "down"
                        print(f"Executing: scroll {direction}", flush=True)
                        await session.call_tool("scroll", {"direction": direction, "amount": 3})
                        await asyncio.sleep(1)

                    elif cmd == "key":
                        key_name = args.strip()
                        print(f"Executing: press key '{key_name}'", flush=True)
                        await session.call_tool("press_key", {"key": key_name})
                        await asyncio.sleep(1)
                        print(f"Key '{key_name}' pressed.", flush=True)

                    elif cmd == "screenshot":
                        label = args.strip() or f"page_{int(time.time())}"
                        label = label.replace(" ", "_").replace("/", "_")
                        print(f"Executing: screenshot '{label}'", flush=True)
                        result = await session.call_tool("screenshot", {"full_page": False})
                        # The result contains ImageContent + TextContent
                        saved = False
                        for content_item in result.content:
                            if hasattr(content_item, 'data') and hasattr(content_item, 'mimeType'):
                                # ImageContent — save base64 PNG to file
                                img_bytes = base64.b64decode(content_item.data)
                                filepath = os.path.join(SCREENSHOT_DIR, f"{label}.png")
                                with open(filepath, "wb") as f:
                                    f.write(img_bytes)
                                abs_path = os.path.abspath(filepath)
                                print(f"SCREENSHOT SAVED: {abs_path}", flush=True)
                                saved = True
                            elif hasattr(content_item, 'text'):
                                info = json.loads(content_item.text)
                                print(f"  URL: {info.get('url', '?')}", flush=True)
                                print(f"  Title: {info.get('title', '?')}", flush=True)
                        if not saved:
                            print("WARNING: No image data in screenshot response.", flush=True)

                    elif cmd == "text":
                        print("Executing: get visible text", flush=True)
                        result = await session.call_tool("get_text", {"max_length": 3000})
                        for content_item in result.content:
                            if hasattr(content_item, 'text'):
                                print(content_item.text, flush=True)

                    elif cmd == "tabs":
                        print("Executing: list tabs", flush=True)
                        result = await session.call_tool("get_tabs", {})
                        for content_item in result.content:
                            if hasattr(content_item, 'text'):
                                tabs_data = json.loads(content_item.text)
                                if isinstance(tabs_data, list):
                                    for i, tab in enumerate(tabs_data):
                                        marker = " *" if tab.get("active") else ""
                                        print(f"  [{i}] {tab.get('title', '?')} — {tab.get('url', '?')}{marker}", flush=True)
                                else:
                                    print(f"  {content_item.text}", flush=True)

                    elif cmd == "switchtab":
                        idx = int(args.strip())
                        print(f"Executing: switch to tab {idx}", flush=True)
                        await session.call_tool("switch_tab", {"index": idx})
                        await asyncio.sleep(1)

                    elif cmd == "closetab":
                        if args.strip():
                            idx = int(args.strip())
                            print(f"Executing: close tab {idx}", flush=True)
                            await session.call_tool("close_tab", {"index": idx})
                        else:
                            print("Executing: close current tab", flush=True)
                            await session.call_tool("close_tab", {})
                        await asyncio.sleep(1)

                    elif cmd == "wait":
                        ms = int(args.strip())
                        print(f"Executing: wait {ms}ms", flush=True)
                        await asyncio.sleep(ms / 1000.0)

                    else:
                        print(f"Unknown command: {cmd}", flush=True)
                        print("Available: navigate, click, type, scroll, key, screenshot, text, tabs, switchtab, closetab, wait, quit", flush=True)

                except Exception as e:
                    print(f"Command execution error: {e}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
