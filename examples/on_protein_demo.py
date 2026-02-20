"""
PredatorBrowser Demo: Amazon India - Optimum Nutrition Protein Powder
=====================================================================
Agent-driven demo: No external LLM API needed.
The browser acts as the "hands", the calling agent acts as the "brain".

Steps:
  0. Open Amazon.in
  1. Type "optimum nutrition protein powder" in the search box
  2. Submit search
  3. Wait for results to load
  4. Click the first product result
  5. Click "Add to Cart"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import base64
from pathlib import Path
from uuid import uuid4
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.v2 import (
    ActionContract,
    ActionSpec,
    ActionType,
    PredatorEngineV2,
    RetryPolicy,
    SecurityPolicy,
    SessionConfig,
    TimeoutPolicy,
    VerificationRule,
    VerificationRuleType,
    WaitCondition,
)

OUT_DIR = Path("/tmp/predator-on-demo")
OUT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOTS_DIR = OUT_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def make_contract(
    workflow_id: str,
    run_id: str,
    step_index: int,
    intent: str,
    action_spec: ActionSpec,
    waits: tuple[WaitCondition, ...] = (),
    rules: tuple[VerificationRule, ...] = (),
    timeout: TimeoutPolicy | None = None,
    retry: RetryPolicy | None = None,
) -> ActionContract:
    return ActionContract(
        workflow_id=workflow_id,
        run_id=run_id,
        step_index=step_index,
        intent=intent,
        action_spec=action_spec,
        wait_conditions=waits,
        verification_rules=rules,
        timeout=timeout or TimeoutPolicy(),
        retry=retry or RetryPolicy(max_attempts=3, initial_backoff_ms=500, max_backoff_ms=3000),
        metadata={"high_risk_approved": False},
    )


async def run() -> None:
    suffix = uuid4().hex[:8]
    workflow_id = f"wf-on-protein-{suffix}"
    run_id = f"run-on-protein-{suffix}"
    tenant_id = "tenant-demo"

    print(f"\n{'='*70}")
    print("  PREDATOR BROWSER — Amazon Protein Powder Demo")
    print(f"  Workflow: {workflow_id}")
    print(f"{'='*70}\n")

    policy = SecurityPolicy(
        allow_domains=("amazon.in", "www.amazon.in"),
        deny_domains=(),
        allow_custom_js=False,
    )

    engine = PredatorEngineV2(
        session_config=SessionConfig(
            headless=False,
            viewport_width=1440,
            viewport_height=900,
            max_total_sessions=2,
        ),
        artifact_root_dir=str(OUT_DIR / "artifacts"),
        audit_root_dir=str(OUT_DIR / "audit"),
        control_db_path=str(OUT_DIR / "control.db"),
        telemetry_dir=str(OUT_DIR / "telemetry"),
    )
    await engine.initialize()
    print("[✓] Engine initialized\n")

    steps: list[dict[str, Any]] = []
    step_counter = 0

    async def exec_step(c: ActionContract, description: str) -> dict[str, Any]:
        nonlocal step_counter
        print(f"  Step {step_counter}: {description}")
        print(f"    Intent: {c.intent}")
        print(f"    Action: {c.action_spec.action_type.value}", end="")
        if c.action_spec.url:
            print(f" → {c.action_spec.url}", end="")
        if c.action_spec.text:
            print(f" → \"{c.action_spec.text}\"", end="")
        print()

        result = await engine.execute_contract(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            policy=policy,
            contract=c,
        )

        step_data = {
            "step": step_counter,
            "intent": c.intent,
            "action_id": result.action_id,
            "success": result.success,
            "failure_code": result.failure_code,
        }
        steps.append(step_data)

        if result.success:
            print(f"    ✅ Success (action_id: {result.action_id[:12]}...)")
        else:
            print(f"    ❌ Failed: {result.failure_code}")

        # Capture structured state after each step
        try:
            state = await engine.get_structured_state(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                policy=policy,
            )
            # Save state to file
            state_path = OUT_DIR / f"state_step_{step_counter}.json"
            with open(state_path, "w") as f:
                json.dump(state, f, indent=2, default=str)
            print(f"    📄 State saved: {state_path.name}")

            # Print some contextual info
            url = state.get("url", "")
            title = state.get("title", "")
            n_elements = len(state.get("interactive_elements", []))
            print(f"    🌐 URL: {url[:80]}")
            print(f"    📝 Title: {title[:60]}")
            print(f"    🔘 Interactive elements: {n_elements}")
        except Exception as e:
            print(f"    ⚠️  Could not get state: {e}")

        print()
        step_counter += 1

        if not result.success:
            raise RuntimeError(f"Step failed: {c.intent} → {result.failure_code}")

        return step_data

    try:
        # ─── Step 0: Open Amazon.in ───
        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=0,
                intent="open_amazon_india",
                action_spec=ActionSpec(
                    action_type=ActionType.NAVIGATE,
                    url="https://www.amazon.in/",
                ),
                waits=(
                    WaitCondition(
                        kind="selector",
                        payload={"selector": "#twotabsearchtextbox", "state": "visible"},
                        timeout_ms=30000,
                    ),
                ),
            ),
            "Navigate to Amazon.in",
        )

        # Let page settle
        await asyncio.sleep(2)

        # ─── Step 1: Type search query ───
        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=1,
                intent="type_search_query",
                action_spec=ActionSpec(
                    action_type=ActionType.TYPE,
                    selector="#twotabsearchtextbox",
                    text="optimum nutrition protein powder",
                ),
                waits=(
                    WaitCondition(
                        kind="function",
                        payload={
                            "expression": "() => { const e = document.querySelector('#twotabsearchtextbox'); return !!e && e.value.toLowerCase().includes('optimum nutrition'); }"
                        },
                        timeout_ms=15000,
                    ),
                ),
            ),
            "Type 'optimum nutrition protein powder' in search box",
        )

        # ─── Step 2: Submit search ───
        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=2,
                intent="submit_search",
                action_spec=ActionSpec(
                    action_type=ActionType.CLICK,
                    selector="#nav-search-submit-button",
                ),
                waits=(
                    WaitCondition(
                        kind="url",
                        payload={"url_pattern": r"/s\?"},
                        timeout_ms=30000,
                    ),
                    WaitCondition(
                        kind="function",
                        payload={
                            "expression": """() => {
                                const hasResults = !!document.querySelector('div[data-component-type="s-search-result"]');
                                const hasCaptcha = !!document.querySelector('form[action*="validateCaptcha"], input#captchacharacters');
                                return hasResults || hasCaptcha;
                            }"""
                        },
                        timeout_ms=45000,
                    ),
                ),
                timeout=TimeoutPolicy(execute_timeout_ms=30000, wait_timeout_ms=45000),
            ),
            "Click search button and wait for results",
        )

        # ─── Step 3: Wait for results to load fully ───
        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=3,
                intent="ensure_results_visible",
                action_spec=ActionSpec(action_type=ActionType.WAIT_ONLY),
                waits=(
                    WaitCondition(
                        kind="function",
                        payload={
                            "expression": """() => {
                                return !!document.querySelector('div[data-component-type="s-search-result"] h2 a');
                            }"""
                        },
                        timeout_ms=60000,
                    ),
                ),
                timeout=TimeoutPolicy(wait_timeout_ms=60000),
            ),
            "Ensure search results are fully loaded",
        )

        # ─── Step 4: Click the first product ───
        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=4,
                intent="open_first_product",
                action_spec=ActionSpec(
                    action_type=ActionType.CLICK,
                    selector_candidates=(
                        'div[data-component-type="s-search-result"][data-asin] h2 a:visible',
                        'div[data-component-type="s-search-result"][data-asin] a.a-link-normal[href*="/dp/"]:visible',
                        'div.s-main-slot a[href*="/dp/"]:visible',
                    ),
                ),
                waits=(
                    WaitCondition(
                        kind="selector",
                        payload={
                            "selector": "#add-to-cart-button, input[name='submit.add-to-cart'], [data-csa-c-slot-id='addToCart'], #buy-now-button",
                            "state": "attached",
                        },
                        timeout_ms=60000,
                    ),
                ),
                timeout=TimeoutPolicy(execute_timeout_ms=30000, wait_timeout_ms=60000),
            ),
            "Click the first search result (product page)",
        )

        # ─── Step 5: Add to cart ───
        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=5,
                intent="add_to_cart",
                action_spec=ActionSpec(
                    action_type=ActionType.CLICK,
                    selector_candidates=(
                        "#add-to-cart-button",
                        'input[name="submit.add-to-cart"]',
                        '[id*="add-to-cart-button"]',
                    ),
                ),
                waits=(
                    WaitCondition(
                        kind="selector",
                        payload={
                            "selector": "#sw-gtc, [data-feature-id='proceed-to-checkout-action'], #NATC_SMART_WAGON_CONF_MSG_SUCCESS, #ewc-content, .a-alert-success, #attach-sidesheet-checkout-button",
                            "state": "attached",
                        },
                        timeout_ms=45000,
                    ),
                ),
                rules=(
                    VerificationRule(
                        rule_type=VerificationRuleType.URL_PATTERN,
                        payload={"pattern": r"(cart|gp/cart|sw/atc|huc|attach)"},
                    ),
                ),
                timeout=TimeoutPolicy(execute_timeout_ms=30000, wait_timeout_ms=45000),
            ),
            "Click 'Add to Cart' button",
        )

        print(f"\n{'='*70}")
        print("  ✅ ALL STEPS COMPLETED SUCCESSFULLY!")
        print(f"{'='*70}\n")

        final = {
            "ok": True,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "steps": steps,
            "health": engine.get_health(),
            "note": "Optimum Nutrition protein powder added to cart on Amazon.in",
        }
        result_path = OUT_DIR / "result.json"
        with open(result_path, "w") as f:
            json.dump(final, f, indent=2, default=str)
        print(f"Results saved to: {result_path}")
        print(json.dumps(final, indent=2, default=str))

    except Exception as exc:
        print(f"\n{'='*70}")
        print(f"  ❌ FAILED at step: {exc}")
        print(f"{'='*70}\n")

        final = {
            "ok": False,
            "error": str(exc),
            "steps": steps,
            "health": engine.get_health(),
            "note": "May have been blocked by login/captcha/modal. Browser was in headed mode.",
        }
        result_path = OUT_DIR / "result.json"
        with open(result_path, "w") as f:
            json.dump(final, f, indent=2, default=str)
        print(json.dumps(final, indent=2, default=str))

    finally:
        print("\nKeeping browser open for 30 seconds so you can observe...")
        await asyncio.sleep(30)
        await engine.close_workflow_session(workflow_id)
        await engine.close()
        print("[✓] Browser closed.")


if __name__ == "__main__":
    asyncio.run(run())
