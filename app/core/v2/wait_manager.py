from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Page, Response

from app.core.v2.contracts import WaitCondition


@dataclass(frozen=True)
class WaitOutcome:
    condition: WaitCondition
    satisfied: bool
    detail: str


@dataclass(frozen=True)
class ChaosPolicy:
    enabled: bool = False
    seed: int = 0
    pre_action_delay_ms_min: int = 0
    pre_action_delay_ms_max: int = 0
    post_action_delay_ms_min: int = 0
    post_action_delay_ms_max: int = 0
    dom_mutation_probability: float = 0.0
    dom_mutation_selector: str = "button,a[href],input,select,textarea"


class WaitManager:
    """Event-driven waiter with no arbitrary sleeps."""

    def __init__(self, page: Page, chaos_policy: ChaosPolicy | None = None) -> None:
        self._page = page
        self._chaos = chaos_policy or ChaosPolicy()
        self._rng = random.Random(self._chaos.seed)

    @property
    def page(self) -> Page:
        return self._page

    async def _maybe_delay(self, minimum_ms: int, maximum_ms: int) -> None:
        if not self._chaos.enabled:
            return
        if minimum_ms < 0 or maximum_ms < 0:
            return
        if maximum_ms < minimum_ms:
            return
        if maximum_ms == 0:
            return
        delay_ms = self._rng.randint(minimum_ms, maximum_ms)
        if delay_ms <= 0:
            return
        await asyncio.sleep(delay_ms / 1000.0)

    async def _maybe_mutate_dom(self) -> None:
        if not self._chaos.enabled:
            return
        if self._chaos.dom_mutation_probability <= 0.0:
            return
        if self._rng.random() > self._chaos.dom_mutation_probability:
            return

        selector = self._chaos.dom_mutation_selector
        target_index = self._rng.randint(0, 20)
        script = """
        ({selector, targetIndex}) => {
            const list = Array.from(document.querySelectorAll(selector));
            if (!list.length) return false;
            const index = Math.min(targetIndex, list.length - 1);
            const target = list[index];
            if (!target) return false;
            target.remove();
            return true;
        }
        """
        try:
            await self._page.evaluate(script, {"selector": selector, "targetIndex": target_index})
        except Exception:
            return

    async def _chaos_pre_action(self) -> None:
        if not self._chaos.enabled:
            return
        await self._maybe_delay(self._chaos.pre_action_delay_ms_min, self._chaos.pre_action_delay_ms_max)
        await self._maybe_mutate_dom()

    async def _chaos_post_action(self) -> None:
        if not self._chaos.enabled:
            return
        await self._maybe_delay(self._chaos.post_action_delay_ms_min, self._chaos.post_action_delay_ms_max)

    async def wait_for_selector(
        self,
        selector: str,
        state: str = "visible",
        timeout_ms: int = 10_000,
        strict: bool = False,
    ) -> bool:
        locator = self._page.locator(selector)
        await locator.wait_for(state=state, timeout=timeout_ms)
        if strict and await locator.count() != 1:
            raise ValueError(f"Selector '{selector}' resolved to != 1 element")
        return True

    async def wait_for_response(
        self,
        url_pattern: str,
        timeout_ms: int = 10_000,
        status_min: int | None = None,
        status_max: int | None = None,
    ) -> Response:
        regex = re.compile(url_pattern)

        def predicate(response: Response) -> bool:
            if not regex.search(response.url):
                return False
            if status_min is not None and response.status < status_min:
                return False
            if status_max is not None and response.status > status_max:
                return False
            return True

        response = await self._page.wait_for_event("response", predicate=predicate, timeout=timeout_ms)
        return response

    async def wait_for_function(
        self,
        expression: str,
        arg: Any = None,
        timeout_ms: int = 10_000,
    ) -> bool:
        await self._page.wait_for_function(expression, arg=arg, timeout=timeout_ms)
        return True

    async def wait_for_url(self, url_pattern: str, timeout_ms: int = 10_000) -> bool:
        regex = re.compile(url_pattern)
        await self._page.wait_for_url(regex, timeout=timeout_ms)
        return True

    async def wait_for_condition(self, condition: WaitCondition) -> WaitOutcome:
        timeout_ms = condition.timeout_ms or condition.payload.get("timeout_ms", 10_000)

        if condition.kind == "selector":
            await self.wait_for_selector(
                selector=condition.payload["selector"],
                state=condition.payload.get("state", "visible"),
                timeout_ms=timeout_ms,
                strict=condition.payload.get("strict", False),
            )
            return WaitOutcome(condition=condition, satisfied=True, detail="selector")

        if condition.kind == "response":
            response = await self.wait_for_response(
                url_pattern=condition.payload["url_pattern"],
                timeout_ms=timeout_ms,
                status_min=condition.payload.get("status_min"),
                status_max=condition.payload.get("status_max"),
            )
            return WaitOutcome(
                condition=condition,
                satisfied=True,
                detail=f"response:{response.status}:{response.url}",
            )

        if condition.kind == "function":
            await self.wait_for_function(
                expression=condition.payload["expression"],
                arg=condition.payload.get("arg"),
                timeout_ms=timeout_ms,
            )
            return WaitOutcome(condition=condition, satisfied=True, detail="function")

        if condition.kind == "url":
            await self.wait_for_url(
                url_pattern=condition.payload["url_pattern"],
                timeout_ms=timeout_ms,
            )
            return WaitOutcome(condition=condition, satisfied=True, detail="url")

        raise ValueError(f"Unsupported wait condition kind: {condition.kind}")

    async def wait_composite(self, conditions: tuple[WaitCondition, ...], mode: str = "all") -> list[WaitOutcome]:
        if not conditions:
            return []

        tasks = [asyncio.create_task(self.wait_for_condition(condition)) for condition in conditions]

        try:
            if mode == "all":
                return list(await asyncio.gather(*tasks))

            if mode == "any":
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                winner = list(done)[0]
                for task in pending:
                    task.cancel()
                return [await winner]

            raise ValueError(f"Unsupported composite mode: {mode}")
        finally:
            await self._cancel_and_drain(tasks)

    async def execute_with_conditions(
        self,
        action: Callable[[], Awaitable[Any]],
        conditions: tuple[WaitCondition, ...],
        mode: str = "all",
    ) -> tuple[Any, list[WaitOutcome]]:
        """Pre-arm waits, run action, then collect deterministic wait outcomes.

        This avoids missing fast responses that can occur if waits are attached only
        after action dispatch.
        """
        if not conditions:
            return await action(), []

        tasks = [asyncio.create_task(self.wait_for_condition(condition)) for condition in conditions]
        try:
            await asyncio.sleep(0)
            await self._chaos_pre_action()
            result = await action()
            await self._chaos_post_action()

            if mode == "all":
                outcomes = list(await asyncio.gather(*tasks))
                return result, outcomes

            if mode == "any":
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                winner = list(done)[0]
                for task in pending:
                    task.cancel()
                return result, [await winner]

            raise ValueError(f"Unsupported composite mode: {mode}")
        finally:
            await self._cancel_and_drain(tasks)

    async def _cancel_and_drain(self, tasks: list[asyncio.Task[Any]]) -> None:
        for task in tasks:
            if task.done() or task.cancelled():
                continue
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
