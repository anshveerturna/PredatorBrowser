from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.v2.contracts import ActionContract, ActionType


@dataclass(frozen=True)
class ContractValidationDecision:
    allowed: bool
    code: str
    detail: str = ""


class ActionContractValidator:
    def __init__(
        self,
        max_selector_length: int = 256,
        max_selector_candidates: int = 8,
        max_text_length: int = 4_096,
        max_js_expression_length: int = 512,
    ) -> None:
        self._max_selector_length = max_selector_length
        self._max_selector_candidates = max_selector_candidates
        self._max_text_length = max_text_length
        self._max_js_expression_length = max_js_expression_length
        self._broad_selectors = {
            "*",
            "body *",
            "html *",
            "body>*",
            "html>*",
            "body > *",
            "html > *",
        }

    def _invalid(self, code: str, detail: str) -> ContractValidationDecision:
        return ContractValidationDecision(allowed=False, code=code, detail=detail)

    def _validate_selector(self, selector: str) -> ContractValidationDecision | None:
        normalized = " ".join(selector.split()).strip().lower()
        if not normalized:
            return self._invalid("INVALID_ACTION_SPEC", "empty selector")
        if len(selector) > self._max_selector_length:
            return self._invalid("INVALID_ACTION_SPEC", "selector exceeds max length")
        if normalized in self._broad_selectors:
            return self._invalid("INVALID_ACTION_SPEC", "selector too broad")
        return None

    def _validate_url(self, url: str) -> ContractValidationDecision | None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return self._invalid("INVALID_ACTION_SPEC", "url must use http/https")
        if not parsed.netloc:
            return self._invalid("INVALID_ACTION_SPEC", "url missing host")
        return None

    def validate(self, contract: ActionContract) -> ContractValidationDecision:
        if contract.step_index < 0:
            return self._invalid("INVALID_CONTRACT", "step_index must be >= 0")

        if not isinstance(contract.metadata.get("high_risk_approved", False), bool):
            return self._invalid("INVALID_CONTRACT", "high_risk_approved must be boolean")

        action = contract.action_spec
        if action.selector:
            selector_result = self._validate_selector(action.selector)
            if selector_result:
                return selector_result

        if len(action.selector_candidates) > self._max_selector_candidates:
            return self._invalid("INVALID_ACTION_SPEC", "too many selector_candidates")
        for selector in action.selector_candidates:
            selector_result = self._validate_selector(selector)
            if selector_result:
                return selector_result

        if action.text and len(action.text) > self._max_text_length:
            return self._invalid("INVALID_ACTION_SPEC", "text exceeds max length")

        if action.url:
            url_result = self._validate_url(action.url)
            if url_result:
                return url_result

        if action.action_type == ActionType.NAVIGATE and not action.url:
            return self._invalid("INVALID_ACTION_SPEC", "navigate action requires url")

        if action.action_type == ActionType.UPLOAD and not action.upload_artifact_id:
            return self._invalid("INVALID_ACTION_SPEC", "upload action requires upload_artifact_id")

        if action.js_expression:
            if len(action.js_expression) > self._max_js_expression_length:
                return self._invalid("INVALID_ACTION_SPEC", "js_expression exceeds max length")

        for wait in contract.wait_conditions:
            if wait.kind not in {"selector", "response", "function", "url"}:
                return self._invalid("INVALID_WAIT_CONDITION", f"unsupported wait kind={wait.kind}")
            timeout = wait.timeout_ms or 0
            if timeout < 0:
                return self._invalid("INVALID_WAIT_CONDITION", "wait timeout must be >= 0")

        return ContractValidationDecision(allowed=True, code="OK")
