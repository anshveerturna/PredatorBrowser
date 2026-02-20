from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

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


OUT_DIR = Path("/tmp/predator-amazon-contextual-demo")
OUT_DIR.mkdir(parents=True, exist_ok=True)


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


def _pick(
    state: dict[str, Any],
    *,
    roles: tuple[str, ...] = (),
    types: tuple[str, ...] = (),
    name_contains: tuple[str, ...] = (),
    enabled_only: bool = True,
    visible_only: bool = True,
) -> dict[str, Any] | None:
    elems = state.get("interactive_elements", [])
    normalized_contains = tuple(item.lower() for item in name_contains)

    for elem in elems:
        role = str(elem.get("role", "")).lower()
        elem_type = str(elem.get("type", "")).lower()
        name = str(elem.get("name_short", "")).lower()
        enabled = bool(elem.get("enabled", False))
        visible = bool(elem.get("visible", False))

        if roles and role not in roles:
            continue
        if types and elem_type not in types:
            continue
        if enabled_only and not enabled:
            continue
        if visible_only and not visible:
            continue
        if normalized_contains and not any(token in name for token in normalized_contains):
            continue
        return elem
    return None


async def run() -> None:
    suffix = uuid4().hex[:8]
    workflow_id = f"wf-amazon-contextual-{suffix}"
    run_id = f"run-amazon-contextual-{suffix}"
    tenant_id = "tenant-demo"
    policy = SecurityPolicy(allow_domains=("amazon.in",), deny_domains=(), allow_custom_js=False)

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

    steps: list[dict[str, Any]] = []
    step_index = 0

    async def exec_step(contract: ActionContract) -> None:
        nonlocal step_index
        result = await engine.execute_contract(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy, contract=contract)
        steps.append(
            {
                "step": contract.step_index,
                "intent": contract.intent,
                "action_id": result.action_id,
                "success": result.success,
                "failure_code": result.failure_code,
                "metadata": result.metadata,
            }
        )
        step_index += 1
        if not result.success:
            raise RuntimeError(f"Step {contract.step_index} failed: {contract.intent} -> {result.failure_code}")

    try:
        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=step_index,
                intent="open_amazon_in",
                action_spec=ActionSpec(action_type=ActionType.NAVIGATE, url="https://www.amazon.in/"),
                waits=(
                    WaitCondition(
                        kind="selector",
                        payload={"selector": "#twotabsearchtextbox", "state": "visible"},
                        timeout_ms=30000,
                    ),
                ),
            )
        )

        state = await engine.get_structured_state(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy)
        search_input = _pick(
            state,
            roles=("input", "textbox"),
            types=("search", "text"),
            name_contains=("search",),
        )

        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=step_index,
                intent="type_query",
                action_spec=(
                    ActionSpec(action_type=ActionType.TYPE, target_eid=search_input["eid"], text="best gaming laptop")
                    if search_input
                    else ActionSpec(action_type=ActionType.TYPE, selector="#twotabsearchtextbox", text="best gaming laptop")
                ),
                waits=(
                    WaitCondition(
                        kind="function",
                        payload={
                            "expression": "() => { const e = document.querySelector('#twotabsearchtextbox'); return !!e && e.value.toLowerCase().includes('gaming laptop'); }"
                        },
                        timeout_ms=15000,
                    ),
                ),
            )
        )

        state = await engine.get_structured_state(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy)
        search_submit = _pick(
            state,
            roles=("button", "input"),
            name_contains=("search",),
        )

        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=step_index,
                intent="submit_search",
                action_spec=(
                    ActionSpec(action_type=ActionType.CLICK, target_eid=search_submit["eid"])
                    if search_submit
                    else ActionSpec(action_type=ActionType.CLICK, selector="#nav-search-submit-button")
                ),
                waits=(
                    WaitCondition(kind="url", payload={"url_pattern": r"/s\?"}, timeout_ms=30000),
                    WaitCondition(
                        kind="function",
                        payload={
                            "expression": "() => !!document.querySelector('div[data-component-type=\"s-search-result\"][data-asin] h2 a')"
                        },
                        timeout_ms=60000,
                    ),
                ),
                timeout=TimeoutPolicy(execute_timeout_ms=30000, wait_timeout_ms=60000),
            )
        )

        state = await engine.get_structured_state(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy)
        sort_box = _pick(
            state,
            roles=("select", "combobox"),
            name_contains=("sort", "featured"),
        )

        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=step_index,
                intent="sort_by_review_rank",
                action_spec=(
                    ActionSpec(action_type=ActionType.SELECT, target_eid=sort_box["eid"], select_value="review-rank")
                    if sort_box
                    else ActionSpec(action_type=ActionType.SELECT, selector="#s-result-sort-select", select_value="review-rank")
                ),
                waits=(
                    WaitCondition(
                        kind="function",
                        payload={
                            "expression": "() => { const e = document.querySelector('#s-result-sort-select'); return !!e && e.value === 'review-rank'; }"
                        },
                        timeout_ms=30000,
                    ),
                ),
                timeout=TimeoutPolicy(execute_timeout_ms=25000, wait_timeout_ms=30000),
            )
        )

        state = await engine.get_structured_state(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy)
        top_product_link = _pick(
            state,
            roles=("a", "link"),
            name_contains=("laptop", "gaming"),
        )

        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=step_index,
                intent="open_top_sorted_product",
                action_spec=(
                    ActionSpec(action_type=ActionType.CLICK, target_eid=top_product_link["eid"])
                    if top_product_link
                    else ActionSpec(
                        action_type=ActionType.CLICK,
                        selector='div[data-component-type="s-search-result"][data-asin] h2 a:visible',
                    )
                ),
                waits=(
                    WaitCondition(
                        kind="selector",
                        payload={"selector": "#add-to-cart-button, input[name='submit.add-to-cart']", "state": "attached"},
                        timeout_ms=60000,
                    ),
                ),
                timeout=TimeoutPolicy(execute_timeout_ms=30000, wait_timeout_ms=60000),
            )
        )

        state = await engine.get_structured_state(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy)
        add_to_cart = _pick(
            state,
            roles=("button", "input"),
            name_contains=("add to cart",),
        )

        await exec_step(
            make_contract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=step_index,
                intent="add_to_cart",
                action_spec=(
                    ActionSpec(action_type=ActionType.CLICK, target_eid=add_to_cart["eid"])
                    if add_to_cart
                    else ActionSpec(action_type=ActionType.CLICK, selector="#add-to-cart-button")
                ),
                waits=(
                    WaitCondition(
                        kind="selector",
                        payload={
                            "selector": "#sw-gtc, [data-feature-id='proceed-to-checkout-action'], #NATC_SMART_WAGON_CONF_MSG_SUCCESS, #ewc-content, .a-alert-success",
                            "state": "attached",
                        },
                        timeout_ms=45000,
                    ),
                ),
                rules=(VerificationRule(rule_type=VerificationRuleType.URL_PATTERN, payload={"pattern": r"(cart|gp/cart|sw/atc|huc)"}),),
                timeout=TimeoutPolicy(execute_timeout_ms=30000, wait_timeout_ms=45000),
            )
        )

        print(
            json.dumps(
                {
                    "ok": True,
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "steps": steps,
                    "health": engine.get_health(),
                    "mode": "contextual_eid_first",
                    "note": "Actions prioritize live structured state EIDs over fixed selectors.",
                },
                indent=2,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "error": str(exc),
                    "steps": steps,
                    "health": engine.get_health(),
                    "mode": "contextual_eid_first",
                },
                indent=2,
            )
        )
    finally:
        await asyncio.sleep(20)
        await engine.close_workflow_session(workflow_id)
        await engine.close()


if __name__ == "__main__":
    asyncio.run(run())
