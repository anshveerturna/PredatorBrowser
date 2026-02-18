from app.core.v2.quota_manager import QuotaManager, TenantQuota
from app.core.v2.resilience import CircuitState, DomainCircuitBreaker


def test_quota_action_rate_limit() -> None:
    manager = QuotaManager(default_quota=TenantQuota(max_actions_per_minute=2))

    assert manager.check_action_rate("tenant-1", now=100.0).allowed is True
    manager.register_action("tenant-1", now=100.0)

    assert manager.check_action_rate("tenant-1", now=110.0).allowed is True
    manager.register_action("tenant-1", now=110.0)

    blocked = manager.check_action_rate("tenant-1", now=120.0)
    assert blocked.allowed is False
    assert blocked.code == "QUOTA_ACTION_RATE"


def test_quota_artifact_limit() -> None:
    manager = QuotaManager(default_quota=TenantQuota(max_artifact_bytes=100))

    assert manager.check_artifact_quota("tenant-1", additional_bytes=60).allowed is True
    manager.register_artifact_bytes("tenant-1", size_bytes=60)

    blocked = manager.check_artifact_quota("tenant-1", additional_bytes=50)
    assert blocked.allowed is False
    assert blocked.code == "QUOTA_ARTIFACT_BYTES"


def test_circuit_breaker_open_then_half_open() -> None:
    breaker = DomainCircuitBreaker(failure_threshold=2, failure_window_seconds=60, open_interval_seconds=30)
    domain = "example.com"

    decision = breaker.allow(domain, now=100.0)
    assert decision.allowed is True

    breaker.record_failure(domain, now=101.0)
    state = breaker.record_failure(domain, now=102.0)
    assert state == CircuitState.OPEN

    blocked = breaker.allow(domain, now=120.0)
    assert blocked.allowed is False
    assert blocked.code == "CIRCUIT_OPEN"

    half_open = breaker.allow(domain, now=140.0)
    assert half_open.allowed is True
    assert half_open.state == CircuitState.HALF_OPEN

    recovered = breaker.record_success(domain)
    assert recovered == CircuitState.CLOSED


def test_circuit_breaker_isolated_by_tenant() -> None:
    breaker = DomainCircuitBreaker(failure_threshold=1, failure_window_seconds=60, open_interval_seconds=30)
    domain = "example.com"

    breaker.record_failure(domain=domain, tenant_id="tenant-a", now=10.0)
    blocked_a = breaker.allow(domain=domain, tenant_id="tenant-a", now=15.0)
    allowed_b = breaker.allow(domain=domain, tenant_id="tenant-b", now=15.0)

    assert blocked_a.allowed is False
    assert blocked_a.code == "CIRCUIT_OPEN"
    assert allowed_b.allowed is True
