"""Interactive REPL for PredatorBrowser. Reads commands from stdin, executes them."""
import asyncio, base64, json, sys, traceback
from pathlib import Path
from app.core.agent_browser import AgentBrowser, AgentBrowserConfig

OUT = Path("/tmp/predator-live")
OUT.mkdir(parents=True, exist_ok=True)

async def main():
    b = AgentBrowser(AgentBrowserConfig(headless=False))
    await b.initialize()
    print("[READY] PredatorBrowser initialized via CDP", flush=True)

    step = 0
    while True:
        print("CMD>", flush=True)
        try:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            cmd = line.strip()
            if not cmd:
                continue
            if cmd == "quit":
                break

            # Parse command
            parts = cmd.split(" ", 1)
            action = parts[0]
            args = parts[1] if len(parts) > 1 else ""

            if action == "navigate":
                r = await b.navigate(args)
                print(f"RESULT: {json.dumps(r)}", flush=True)

            elif action == "screenshot":
                step += 1
                ss = await b.screenshot()
                path = OUT / f"step{step:02d}.png"
                with open(path, "wb") as f:
                    f.write(base64.b64decode(ss))
                print(f"SCREENSHOT: {path}", flush=True)

            elif action == "state":
                state = await b.get_state()
                # Print summary
                print(f"STATE_URL: {state['url']}", flush=True)
                print(f"STATE_TITLE: {state['title']}", flush=True)
                print(f"STATE_ELEMENTS: {state['element_count']}", flush=True)
                print(f"STATE_SCROLL: {json.dumps(state['scroll'])}", flush=True)
                for e in state['elements']:
                    eid = e['id']
                    tag = e['tag']
                    etype = e.get('type', '')
                    name = (e.get('name', '') or '')[:80]
                    href = (e.get('href', '') or '')[:60]
                    bbox = e.get('bbox', {})
                    print(f"  EL[{eid}] {tag}/{etype} \"{name}\" href={href} bbox={json.dumps(bbox)}", flush=True)
                print("STATE_END", flush=True)

            elif action == "click":
                # click 5  OR  click x=100,y=200
                if args.startswith("x="):
                    coords = dict(p.split("=") for p in args.split(","))
                    r = await b.click(x=int(coords['x']), y=int(coords['y']))
                else:
                    r = await b.click(element_id=int(args))
                print(f"RESULT: {json.dumps(r)}", flush=True)

            elif action == "type":
                # type 5 some text here  OR  type some text here
                try:
                    eid = int(args.split(" ", 1)[0])
                    text = args.split(" ", 1)[1]
                    r = await b.type_text(text, element_id=eid)
                except (ValueError, IndexError):
                    r = await b.type_text(args)
                print(f"RESULT: {json.dumps(r)}", flush=True)

            elif action == "enter":
                r = await b.press_key("Enter")
                print(f"RESULT: {json.dumps(r)}", flush=True)

            elif action == "key":
                r = await b.press_key(args)
                print(f"RESULT: {json.dumps(r)}", flush=True)

            elif action == "scroll":
                direction = args if args else "down"
                r = await b.scroll(direction=direction, amount=3)
                print(f"RESULT: {json.dumps(r)}", flush=True)

            elif action == "wait":
                ms = int(args) if args else 2000
                await b.wait(ms=ms)
                print(f"RESULT: waited {ms}ms", flush=True)

            elif action == "text":
                r = await b.get_text(max_length=int(args) if args else 2000)
                print(f"TEXT: {r['text']}", flush=True)
                print("TEXT_END", flush=True)

            elif action == "eval":
                r = await b._page.evaluate(args)
                print(f"EVAL: {r}", flush=True)

            else:
                print(f"ERROR: unknown command: {action}", flush=True)

        except Exception as e:
            traceback.print_exc()
            print(f"ERROR: {e}", flush=True)

    await b.close()
    print("[DONE]", flush=True)

asyncio.run(main())
