from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from app.core.v2.contracts import VerificationRule, VerificationRuleType
from app.core.v2.network_observer import NetworkObserver
from app.core.v2.state_models import StructuredState


@dataclass(frozen=True)
class VerificationFailure:
    rule_type: str
    severity: str
    code: str
    detail: str


@dataclass(frozen=True)
class VerificationReport:
    passed: bool
    failures: tuple[VerificationFailure, ...]


class VerificationEngine:
    def __init__(self, page: Page, network: NetworkObserver) -> None:
        self._page = page
        self._network = network

    async def _assert_element_present(self, rule: VerificationRule, state: StructuredState) -> VerificationFailure | None:
        eid = str(rule.payload["eid"])
        exists = any(element.eid == eid for element in state.interactive_elements)
        if exists:
            return None
        return VerificationFailure(
            rule_type=rule.rule_type.value,
            severity=rule.severity,
            code="ELEMENT_NOT_PRESENT",
            detail=f"Element '{eid}' not found",
        )

    async def _assert_text_state(self, rule: VerificationRule) -> VerificationFailure | None:
        selector = str(rule.payload["selector"])
        expected = str(rule.payload.get("expected", ""))
        mode = str(rule.payload.get("mode", "contains"))
        locator = self._page.locator(selector).first
        text = (await locator.inner_text()).strip()

        matched = expected in text if mode == "contains" else text == expected
        if matched:
            return None

        return VerificationFailure(
            rule_type=rule.rule_type.value,
            severity=rule.severity,
            code="TEXT_STATE_MISMATCH",
            detail=f"selector={selector}, expected={expected}, actual={text}",
        )

    async def _assert_attribute_state(self, rule: VerificationRule) -> VerificationFailure | None:
        selector = str(rule.payload["selector"])
        attr = str(rule.payload["attribute"])
        expected = rule.payload.get("expected")
        actual = await self._page.locator(selector).first.get_attribute(attr)

        if str(actual) == str(expected):
            return None

        return VerificationFailure(
            rule_type=rule.rule_type.value,
            severity=rule.severity,
            code="ATTRIBUTE_STATE_MISMATCH",
            detail=f"selector={selector}, attr={attr}, expected={expected}, actual={actual}",
        )

    async def _assert_network_status(self, rule: VerificationRule) -> VerificationFailure | None:
        status_min = int(rule.payload.get("status_min", 200))
        status_max = int(rule.payload.get("status_max", 299))
        url_pattern = rule.payload.get("url_pattern")

        events = self._network.events_since(int(rule.payload.get("since_seq", 0)))
        responses = [event for event in events if event.kind == "response"]

        if url_pattern:
            regex = re.compile(str(url_pattern))
            responses = [event for event in responses if regex.search(event.url)]

        matched = any(event.status is not None and status_min <= event.status <= status_max for event in responses)
        if matched:
            return None

        return VerificationFailure(
            rule_type=rule.rule_type.value,
            severity=rule.severity,
            code="NETWORK_STATUS_MISMATCH",
            detail=f"No response with status between {status_min} and {status_max}",
        )

    async def _assert_json_field(self, rule: VerificationRule) -> VerificationFailure | None:
        # Conservative: verify silent backend failures did not occur for target route key.
        route_key = str(rule.payload["route_key"])
        require_no_silent_failure = bool(rule.payload.get("require_no_silent_failure", True))
        if not require_no_silent_failure:
            return None

        for event in self._network.events_since(int(rule.payload.get("since_seq", 0))):
            if event.kind == "response" and event.route_key == route_key and event.silent_failure:
                return VerificationFailure(
                    rule_type=rule.rule_type.value,
                    severity=rule.severity,
                    code="JSON_FIELD_FAILURE_SIGNAL",
                    detail=f"Silent failure signal detected for route_key={route_key}",
                )

        return None

    async def _assert_file_exists(self, rule: VerificationRule) -> VerificationFailure | None:
        path = Path(str(rule.payload["path"]))
        min_size = int(rule.payload.get("min_size", 1))

        if not path.exists():
            return VerificationFailure(
                rule_type=rule.rule_type.value,
                severity=rule.severity,
                code="FILE_NOT_FOUND",
                detail=f"{path}",
            )

        size = path.stat().st_size
        if size < min_size:
            return VerificationFailure(
                rule_type=rule.rule_type.value,
                severity=rule.severity,
                code="FILE_TOO_SMALL",
                detail=f"size={size}, min_size={min_size}",
            )

        return None

    async def _assert_url_pattern(self, rule: VerificationRule) -> VerificationFailure | None:
        pattern = str(rule.payload["pattern"])
        if re.search(pattern, self._page.url):
            return None
        return VerificationFailure(
            rule_type=rule.rule_type.value,
            severity=rule.severity,
            code="URL_PATTERN_MISMATCH",
            detail=f"pattern={pattern}, url={self._page.url}",
        )

    async def _assert_invariant(self, rule: VerificationRule, state: StructuredState) -> VerificationFailure | None:
        invariant = str(rule.payload.get("name", ""))
        if invariant == "no_visible_errors" and state.visible_errors:
            return VerificationFailure(
                rule_type=rule.rule_type.value,
                severity=rule.severity,
                code="INVARIANT_VIOLATION",
                detail="visible_errors_present",
            )
        return None

    async def verify(self, rules: tuple[VerificationRule, ...], state: StructuredState) -> VerificationReport:
        failures: list[VerificationFailure] = []

        for rule in rules:
            failure: VerificationFailure | None = None

            if rule.rule_type == VerificationRuleType.ELEMENT_PRESENT:
                failure = await self._assert_element_present(rule, state)
            elif rule.rule_type == VerificationRuleType.TEXT_STATE:
                failure = await self._assert_text_state(rule)
            elif rule.rule_type == VerificationRuleType.ATTRIBUTE_STATE:
                failure = await self._assert_attribute_state(rule)
            elif rule.rule_type == VerificationRuleType.NETWORK_STATUS:
                failure = await self._assert_network_status(rule)
            elif rule.rule_type == VerificationRuleType.JSON_FIELD:
                failure = await self._assert_json_field(rule)
            elif rule.rule_type == VerificationRuleType.FILE_EXISTS:
                failure = await self._assert_file_exists(rule)
            elif rule.rule_type == VerificationRuleType.URL_PATTERN:
                failure = await self._assert_url_pattern(rule)
            elif rule.rule_type == VerificationRuleType.INVARIANT:
                failure = await self._assert_invariant(rule, state)

            if failure:
                failures.append(failure)

        hard_failures = [failure for failure in failures if failure.severity == "hard"]
        return VerificationReport(
            passed=not hard_failures,
            failures=tuple(failures),
        )
