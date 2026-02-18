from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.core.v2.contracts import ActionType


@dataclass(frozen=True)
class SecurityPolicy:
    allow_domains: tuple[str, ...]
    deny_domains: tuple[str, ...] = ()
    allow_custom_js: bool = False
    high_risk_actions: tuple[ActionType, ...] = (
        ActionType.CUSTOM_JS_RESTRICTED,
        ActionType.UPLOAD,
        ActionType.DOWNLOAD_TRIGGER,
    )
    rate_limit_per_minute: int = 120


@dataclass(frozen=True)
class SecurityDecision:
    allowed: bool
    code: str
    detail: str = ""


class SecurityLayer:
    def __init__(self, policy: SecurityPolicy) -> None:
        self._policy = policy

    def _domain_allowed(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()

        for denied in self._policy.deny_domains:
            denied_host = denied.lower()
            if host == denied_host or host.endswith(f".{denied_host}"):
                return False

        if not self._policy.allow_domains:
            return False

        for allowed in self._policy.allow_domains:
            allowed_host = allowed.lower()
            if host == allowed_host or host.endswith(f".{allowed_host}"):
                return True

        return False

    def evaluate_navigation(self, url: str) -> SecurityDecision:
        if not self._domain_allowed(url):
            return SecurityDecision(
                allowed=False,
                code="SECURITY_DOMAIN_BLOCK",
                detail=f"navigation blocked for url={url}",
            )

        return SecurityDecision(allowed=True, code="OK")

    def evaluate_action(
        self,
        action_type: ActionType,
        current_url: str,
        metadata: dict[str, object] | None = None,
    ) -> SecurityDecision:
        # Navigation target is validated separately; do not block by current URL.
        if action_type != ActionType.NAVIGATE and not self._domain_allowed(current_url):
            return SecurityDecision(
                allowed=False,
                code="SECURITY_DOMAIN_BLOCK",
                detail=f"action blocked outside policy domain: {current_url}",
            )

        request_metadata = metadata or {}
        if action_type in self._policy.high_risk_actions:
            approved = bool(request_metadata.get("high_risk_approved", False))
            if not approved:
                return SecurityDecision(
                    allowed=False,
                    code="SECURITY_APPROVAL_REQUIRED",
                    detail=f"action_type={action_type.value} requires explicit approval",
                )

        if action_type == ActionType.CUSTOM_JS_RESTRICTED and not self._policy.allow_custom_js:
            return SecurityDecision(
                allowed=False,
                code="SECURITY_JS_BLOCKED",
                detail="custom js execution is disabled by policy",
            )

        return SecurityDecision(allowed=True, code="OK")
