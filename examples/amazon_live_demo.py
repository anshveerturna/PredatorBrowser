from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
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


OUT_DIR = Path('/tmp/predator-amazon-demo')
OUT_DIR.mkdir(parents=True, exist_ok=True)


def contract(
    workflow_id: str,
    run_id: str,
    step_index: int,
    intent: str,
    action_spec: ActionSpec,
    wait_conditions: tuple[WaitCondition, ...] = (),
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
        wait_conditions=wait_conditions,
        verification_rules=rules,
        timeout=timeout or TimeoutPolicy(),
        retry=retry or RetryPolicy(),
        metadata={"high_risk_approved": False},
    )


async def run() -> None:
    suffix = uuid4().hex[:8]
    workflow_id = f'wf-amazon-live-{suffix}'
    run_id = f'run-amazon-live-{suffix}'

    engine = PredatorEngineV2(
        session_config=SessionConfig(
            headless=False,
            viewport_width=1440,
            viewport_height=900,
            max_total_sessions=2,
        ),
        artifact_root_dir=str(OUT_DIR / 'artifacts'),
        audit_root_dir=str(OUT_DIR / 'audit'),
        control_db_path=str(OUT_DIR / 'control.db'),
        telemetry_dir=str(OUT_DIR / 'telemetry'),
    )
    await engine.initialize()

    policy = SecurityPolicy(
        allow_domains=('amazon.in',),
        deny_domains=(),
        allow_custom_js=False,
    )

    steps: list[dict[str, Any]] = []

    async def exec_step(c: ActionContract) -> None:
        r = await engine.execute_contract('tenant-demo', workflow_id, policy, c)
        steps.append({
            'step': c.step_index,
            'intent': c.intent,
            'action_id': r.action_id,
            'success': r.success,
            'failure_code': r.failure_code,
            'metadata': r.metadata,
        })
        if not r.success:
            raise RuntimeError(f"Step {c.step_index} failed: {c.intent} -> {r.failure_code}")

    try:
        await exec_step(contract(
            workflow_id,
            run_id,
            0,
            'open_amazon_home',
            ActionSpec(action_type=ActionType.NAVIGATE, url='https://www.amazon.in/'),
            wait_conditions=(
                WaitCondition(kind='selector', payload={'selector': '#twotabsearchtextbox', 'state': 'visible'}, timeout_ms=20000),
            ),
        ))

        await exec_step(contract(
            workflow_id,
            run_id,
            1,
            'type_query',
            ActionSpec(action_type=ActionType.TYPE, selector='#twotabsearchtextbox', text='best gaming laptop'),
            wait_conditions=(
                WaitCondition(kind='function', payload={'expression': "() => document.querySelector('#twotabsearchtextbox') && document.querySelector('#twotabsearchtextbox').value.toLowerCase().includes('gaming laptop')"}, timeout_ms=10000),
            ),
        ))

        await exec_step(contract(
            workflow_id,
            run_id,
            2,
            'submit_search',
            ActionSpec(action_type=ActionType.CLICK, selector='#nav-search-submit-button'),
            wait_conditions=(
                WaitCondition(kind='url', payload={'url_pattern': r'/s\\?'}, timeout_ms=20000),
                WaitCondition(
                    kind='function',
                    payload={
                        'expression': """() => {
                            const hasResults = !!document.querySelector('div[data-component-type="s-search-result"]');
                            const hasCaptcha = !!document.querySelector('form[action*="validateCaptcha"], input#captchacharacters');
                            return hasResults || hasCaptcha;
                        }"""
                    },
                    timeout_ms=45000,
                ),
            ),
            timeout=TimeoutPolicy(execute_timeout_ms=30000, wait_timeout_ms=45000),
            retry=RetryPolicy(max_attempts=3, initial_backoff_ms=500, max_backoff_ms=3000),
        ))

        await exec_step(contract(
            workflow_id,
            run_id,
            3,
            'ensure_results_visible',
            ActionSpec(action_type=ActionType.WAIT_ONLY),
            wait_conditions=(
                WaitCondition(
                    kind='function',
                    payload={
                        'expression': """() => {
                            const hasResults = !!document.querySelector('div[data-component-type="s-search-result"] h2 a');
                            return hasResults;
                        }"""
                    },
                    timeout_ms=120000,
                ),
            ),
            timeout=TimeoutPolicy(wait_timeout_ms=120000),
        ))

        await exec_step(contract(
            workflow_id,
            run_id,
            4,
            'sort_by_customer_review',
            ActionSpec(action_type=ActionType.SELECT, selector='#s-result-sort-select', select_value='review-rank'),
            wait_conditions=(
                WaitCondition(
                    kind='function',
                    payload={'expression': "() => { const el = document.querySelector('#s-result-sort-select'); return !!el && el.value === 'review-rank'; }"},
                    timeout_ms=30000,
                ),
                WaitCondition(
                    kind='function',
                    payload={'expression': "() => !!document.querySelector('div[data-component-type=\"s-search-result\"][data-asin] h2 a')"},
                    timeout_ms=45000,
                ),
            ),
            timeout=TimeoutPolicy(execute_timeout_ms=20000, wait_timeout_ms=45000),
            retry=RetryPolicy(max_attempts=3, initial_backoff_ms=500, max_backoff_ms=3000),
        ))

        await exec_step(contract(
            workflow_id,
            run_id,
            5,
            'open_top_sorted_result',
            ActionSpec(action_type=ActionType.CLICK, selector_candidates=(
                'div[data-component-type="s-search-result"][data-asin] h2 a:visible',
                'div[data-component-type="s-search-result"][data-asin] a.a-link-normal[href*="/dp/"]:visible',
                'div.s-main-slot a[href*="/dp/"]:visible',
            )),
            wait_conditions=(
                WaitCondition(kind='selector', payload={'selector': '#add-to-cart-button, input[name="submit.add-to-cart"], [data-csa-c-slot-id="addToCart"]', 'state': 'attached'}, timeout_ms=45000),
            ),
            timeout=TimeoutPolicy(execute_timeout_ms=30000, wait_timeout_ms=45000),
            retry=RetryPolicy(max_attempts=3, initial_backoff_ms=500, max_backoff_ms=3000),
        ))

        await exec_step(contract(
            workflow_id,
            run_id,
            6,
            'add_to_cart',
            ActionSpec(action_type=ActionType.CLICK, selector_candidates=(
                '#add-to-cart-button',
                'input[name="submit.add-to-cart"]',
                '[id*="add-to-cart-button"]',
            )),
            wait_conditions=(
                WaitCondition(kind='selector', payload={'selector': '#sw-gtc, [data-feature-id="proceed-to-checkout-action"], #NATC_SMART_WAGON_CONF_MSG_SUCCESS, #ewc-content, .a-alert-success', 'state': 'attached'}, timeout_ms=45000),
            ),
            rules=(
                VerificationRule(rule_type=VerificationRuleType.URL_PATTERN, payload={'pattern': r'(cart|gp/cart|sw/atc|huc)'}),
            ),
            timeout=TimeoutPolicy(execute_timeout_ms=30000, wait_timeout_ms=45000),
            retry=RetryPolicy(max_attempts=3, initial_backoff_ms=500, max_backoff_ms=3000),
        ))

        out = {
            'ok': True,
            'steps': steps,
            'health': engine.get_health(),
            'note': 'If Amazon asked for login/captcha/2FA, manual intervention may have been required.'
        }
        print(json.dumps(out, indent=2))

    except Exception as exc:
        out = {
            'ok': False,
            'error': str(exc),
            'steps': steps,
            'health': engine.get_health(),
            'note': 'Likely blocked by login/captcha/modal. Browser was launched in headed mode.'
        }
        print(json.dumps(out, indent=2))
    finally:
        # Keep session open briefly so user can observe end state if window is visible.
        await asyncio.sleep(20)
        await engine.close_workflow_session(workflow_id)
        await engine.close()


if __name__ == '__main__':
    asyncio.run(run())
