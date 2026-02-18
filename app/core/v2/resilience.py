from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum

from app.core.v2.control_plane_store import ControlPlaneStore


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitDecision:
    allowed: bool
    state: CircuitState
    code: str
    detail: str = ""


@dataclass
class DomainCircuit:
    state: CircuitState = CircuitState.CLOSED
    opened_at: float = 0.0
    recent_failures: deque[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.recent_failures is None:
            self.recent_failures = deque()


class DomainCircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        failure_window_seconds: int = 120,
        open_interval_seconds: int = 60,
        store: ControlPlaneStore | None = None,
    ) -> None:
        self._threshold = failure_threshold
        self._window = failure_window_seconds
        self._open_interval = open_interval_seconds
        self._store = store
        self._circuits: dict[str, DomainCircuit] = defaultdict(DomainCircuit)

    def _key(self, domain: str, tenant_id: str | None = None) -> str:
        if tenant_id:
            return f"{tenant_id}::{domain}"
        return domain

    def _prune(self, key: str, now: float) -> None:
        circuit = self._circuits[key]
        cutoff = now - self._window
        while circuit.recent_failures and circuit.recent_failures[0] < cutoff:
            circuit.recent_failures.popleft()

    def allow(self, domain: str, tenant_id: str | None = None, now: float | None = None) -> CircuitDecision:
        now_ts = now if now is not None else time.time()
        key = self._key(domain=domain, tenant_id=tenant_id)
        if self._store:
            snap = self._store.get_circuit(domain=domain, tenant_id=tenant_id)
            state = CircuitState(snap.state)
            opened_at = snap.opened_at

            if state == CircuitState.OPEN:
                if (now_ts - opened_at) >= self._open_interval:
                    self._store.set_circuit(
                        domain=domain,
                        tenant_id=tenant_id,
                        state=CircuitState.HALF_OPEN.value,
                        opened_at=opened_at,
                    )
                    return CircuitDecision(True, CircuitState.HALF_OPEN, "CIRCUIT_HALF_OPEN")
                return CircuitDecision(False, state, "CIRCUIT_OPEN", "domain temporarily blocked")

            return CircuitDecision(True, state, "OK")

        circuit = self._circuits[key]

        if circuit.state == CircuitState.OPEN:
            if (now_ts - circuit.opened_at) >= self._open_interval:
                circuit.state = CircuitState.HALF_OPEN
                return CircuitDecision(True, circuit.state, "CIRCUIT_HALF_OPEN")
            return CircuitDecision(False, circuit.state, "CIRCUIT_OPEN", "domain temporarily blocked")

        return CircuitDecision(True, circuit.state, "OK")

    def record_failure(self, domain: str, tenant_id: str | None = None, now: float | None = None) -> CircuitState:
        now_ts = now if now is not None else time.time()
        key = self._key(domain=domain, tenant_id=tenant_id)
        if self._store:
            snap = self._store.get_circuit(domain=domain, tenant_id=tenant_id)
            state = CircuitState(snap.state)
            self._store.add_circuit_failure(domain=domain, ts=now_ts, tenant_id=tenant_id)
            self._store.prune_circuit_failures(domain=domain, before_ts=now_ts - self._window, tenant_id=tenant_id)

            count = self._store.count_circuit_failures(domain=domain, since_ts=now_ts - self._window, tenant_id=tenant_id)
            if count >= self._threshold or state == CircuitState.HALF_OPEN:
                self._store.set_circuit(
                    domain=domain,
                    tenant_id=tenant_id,
                    state=CircuitState.OPEN.value,
                    opened_at=now_ts,
                )
                return CircuitState.OPEN
            return state

        circuit = self._circuits[key]
        self._prune(key, now_ts)
        circuit.recent_failures.append(now_ts)

        if len(circuit.recent_failures) >= self._threshold:
            circuit.state = CircuitState.OPEN
            circuit.opened_at = now_ts
        elif circuit.state == CircuitState.HALF_OPEN:
            circuit.state = CircuitState.OPEN
            circuit.opened_at = now_ts

        return circuit.state

    def record_success(self, domain: str, tenant_id: str | None = None) -> CircuitState:
        key = self._key(domain=domain, tenant_id=tenant_id)
        if self._store:
            snap = self._store.get_circuit(domain=domain, tenant_id=tenant_id)
            state = CircuitState(snap.state)
            if state == CircuitState.HALF_OPEN:
                self._store.set_circuit(
                    domain=domain,
                    tenant_id=tenant_id,
                    state=CircuitState.CLOSED.value,
                    opened_at=0.0,
                )
                self._store.clear_circuit_failures(domain=domain, tenant_id=tenant_id)
                return CircuitState.CLOSED
            return state

        circuit = self._circuits[key]
        if circuit.state == CircuitState.HALF_OPEN:
            circuit.state = CircuitState.CLOSED
            circuit.recent_failures.clear()
        return circuit.state

    def snapshot(self) -> dict[str, dict[str, object]]:
        if self._store:
            # SQLite-backed snapshot query using in-memory compatibility format.
            # This mirrors the in-memory output contract used by HealthMonitor.
            payload: dict[str, dict[str, object]] = {}
            for domain in self._store.list_circuit_domains():
                snap = self._store.get_circuit(domain=domain)
                count = self._store.count_circuit_failures(domain=domain, since_ts=time.time() - self._window)
                payload[domain] = {
                    "state": snap.state,
                    "recent_failures": count,
                    "opened_at": snap.opened_at,
                }
            return payload

        payload: dict[str, dict[str, object]] = {}
        now_ts = time.time()
        for domain, circuit in self._circuits.items():
            self._prune(domain, now_ts)
            payload[domain] = {
                "state": circuit.state.value,
                "recent_failures": len(circuit.recent_failures),
                "opened_at": circuit.opened_at,
            }
        return payload


@dataclass(frozen=True)
class EngineHealth:
    status: str
    active_sessions: int
    open_circuits: int
    details: dict[str, object]


class HealthMonitor:
    def evaluate(self, active_sessions: int, circuit_snapshot: dict[str, dict[str, object]]) -> EngineHealth:
        open_circuits = sum(1 for value in circuit_snapshot.values() if value.get("state") == CircuitState.OPEN.value)
        status = "healthy"
        if open_circuits > 0:
            status = "degraded"
        if open_circuits > 5:
            status = "unhealthy"
        return EngineHealth(
            status=status,
            active_sessions=active_sessions,
            open_circuits=open_circuits,
            details={"circuits": circuit_snapshot},
        )
