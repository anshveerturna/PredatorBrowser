from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
from playwright.async_api import Page

from app.core.v2.state_models import StructuredState


@dataclass(frozen=True)
class ActionCandidate:
    description: str
    method: str
    selector: str | None
    confidence: float
    metadata: dict[str, Any]


class PerceptionAdapter(Protocol):
    async def observe(self, intent: str, page: Page, state: StructuredState) -> list[ActionCandidate]:
        ...

    async def extract(self, instruction: str, page: Page) -> dict[str, Any]:
        ...


class LocalPerceptionAdapter:
    """Token-efficient local fallback that mimics Stagehand observe() semantics."""

    def _tokens(self, text: str) -> set[str]:
        return {tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if tok and len(tok) > 1}

    def _score(self, intent: str, role: str, name: str, selector_hints: tuple[str, ...]) -> float:
        intent_tokens = self._tokens(intent)
        haystack = " ".join((role, name, " ".join(selector_hints)))
        elem_tokens = self._tokens(haystack)
        if not intent_tokens:
            return 0.0
        overlap = len(intent_tokens.intersection(elem_tokens)) / len(intent_tokens)
        return max(0.0, min(1.0, overlap))

    async def observe(self, intent: str, page: Page, state: StructuredState) -> list[ActionCandidate]:
        del page
        ranked: list[ActionCandidate] = []
        for element in state.interactive_elements:
            selector = element.selector_hints[0] if element.selector_hints else None
            score = self._score(intent, element.role, element.name_short, element.selector_hints)
            if score <= 0:
                continue
            method = "type" if element.element_type in {"input", "textarea", "email", "password"} else "click"
            ranked.append(
                ActionCandidate(
                    description=f"{element.name_short or element.role} ({element.element_type})",
                    method=method,
                    selector=selector,
                    confidence=round((0.7 * score) + (0.3 * element.stability_score), 4),
                    metadata={"eid": element.eid, "role": element.role, "stability": element.stability_score},
                )
            )
        ranked.sort(key=lambda c: c.confidence, reverse=True)
        return ranked[:12]

    async def extract(self, instruction: str, page: Page) -> dict[str, Any]:
        script = (
            r"""
            (instruction) => {
              const text = (document.body?.innerText || '').replace(/\s+/g, ' ').trim();
              return {
                instruction,
                title: document.title || '',
                url: location.href,
                snippet: text.slice(0, 600)
              };
            }
            """
        )
        return await page.evaluate(script, instruction)


class StagehandHttpPerceptionAdapter:
    """Perception-only Stagehand bridge.

    This adapter assumes a sidecar service exposes `/observe` and `/extract`
    that internally call Stagehand's observe()/extract().
    """

    def __init__(self, endpoint: str, api_key: str | None = None, timeout_s: float = 15.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    async def observe(self, intent: str, page: Page, state: StructuredState) -> list[ActionCandidate]:
        payload = {
            "intent": intent,
            "url": page.url,
            "state": state.to_model_dict(),
            "mode": "perception_only",
            "allowed_stagehand_methods": ["observe", "extract"],
            "disallowed_stagehand_methods": ["act", "agent"],
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout_s)
        async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
            async with session.post(f"{self._endpoint}/observe", json=payload) as response:
                response.raise_for_status()
                body = await response.json()
        items = body.get("candidates", [])
        return [
            ActionCandidate(
                description=str(item.get("description", "")),
                method=str(item.get("method", "click")),
                selector=item.get("selector"),
                confidence=float(item.get("confidence", 0.0)),
                metadata=dict(item.get("metadata", {})),
            )
            for item in items
        ]

    async def extract(self, instruction: str, page: Page) -> dict[str, Any]:
        payload = {
            "instruction": instruction,
            "url": page.url,
            "mode": "perception_only",
            "allowed_stagehand_methods": ["extract"],
            "disallowed_stagehand_methods": ["act", "agent"],
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout_s)
        async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
            async with session.post(f"{self._endpoint}/extract", json=payload) as response:
                response.raise_for_status()
                return dict(await response.json())



class ResilientPerceptionAdapter:
    """Use Stagehand adapter when available; fall back to local perception on failures."""

    def __init__(self, primary: PerceptionAdapter, fallback: PerceptionAdapter) -> None:
        self._primary = primary
        self._fallback = fallback

    async def observe(self, intent: str, page: Page, state: StructuredState) -> list[ActionCandidate]:
        try:
            return await self._primary.observe(intent=intent, page=page, state=state)
        except Exception:
            return await self._fallback.observe(intent=intent, page=page, state=state)

    async def extract(self, instruction: str, page: Page) -> dict[str, Any]:
        try:
            return await self._primary.extract(instruction=instruction, page=page)
        except Exception:
            return await self._fallback.extract(instruction=instruction, page=page)


def build_perception_adapter() -> PerceptionAdapter:
    endpoint = os.getenv("PREDATOR_STAGEHAND_ENDPOINT", "").strip()
    fallback = LocalPerceptionAdapter()
    if endpoint:
        primary = StagehandHttpPerceptionAdapter(
            endpoint=endpoint,
            api_key=os.getenv("PREDATOR_STAGEHAND_API_KEY"),
            timeout_s=float(os.getenv("PREDATOR_STAGEHAND_TIMEOUT_S", "15")),
        )
        return ResilientPerceptionAdapter(primary=primary, fallback=fallback)
    return fallback
