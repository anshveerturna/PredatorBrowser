from app.core.v2.contracts import ActionType
from app.core.v2.security_layer import SecurityLayer, SecurityPolicy


def test_domain_allowlist_permits_subdomain() -> None:
    layer = SecurityLayer(SecurityPolicy(allow_domains=("example.com",)))
    decision = layer.evaluate_navigation("https://app.example.com/dashboard")
    assert decision.allowed is True


def test_domain_denylist_blocks_exact_domain() -> None:
    layer = SecurityLayer(SecurityPolicy(allow_domains=("example.com",), deny_domains=("evil.example.com",)))
    decision = layer.evaluate_navigation("https://evil.example.com/phishing")
    assert decision.allowed is False
    assert decision.code == "SECURITY_DOMAIN_BLOCK"


def test_custom_js_blocked_when_disabled() -> None:
    layer = SecurityLayer(SecurityPolicy(allow_domains=("example.com",), allow_custom_js=False))
    decision = layer.evaluate_action(
        ActionType.CUSTOM_JS_RESTRICTED,
        "https://example.com",
        metadata={"high_risk_approved": True},
    )
    assert decision.allowed is False
    assert decision.code == "SECURITY_JS_BLOCKED"


def test_high_risk_action_requires_approval() -> None:
    layer = SecurityLayer(SecurityPolicy(allow_domains=("example.com",)))
    decision = layer.evaluate_action(ActionType.UPLOAD, "https://example.com")
    assert decision.allowed is False
    assert decision.code == "SECURITY_APPROVAL_REQUIRED"
