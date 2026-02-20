from __future__ import annotations

import asyncio
import os
import resource
import time
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256
from typing import Any, Protocol

from app.core.v2.contracts import ActionContract, ActionExecutionResult, ActionType
from app.core.v2.predator_v2 import PredatorEngineV2
from app.core.v2.quota_manager import TenantQuota
from app.core.v2.security_layer import SecurityPolicy
from app.core.v2.session_manager import SessionConfig


class WorkClass(str, Enum):
    LIGHT = "light"
    HEAVY = "heavy"


@dataclass(frozen=True)
class NodeAdmissionSLO:
    max_active_sessions: int = 120
    max_inflight_actions: int = 120
    max_loop_lag_p95_ms: float = 1_200.0
    max_fd_count: int = 1_024
    max_rss_mb: float = 1_024.0
    max_breaker_open_ratio: float = 0.50


@dataclass(frozen=True)
class ClusterSchedulerConfig:
    shard_count: int = 3
    dispatch_interval_ms: int = 20
    monitor_interval_ms: int = 250
    light_weight: int = 3
    heavy_weight: int = 1


@dataclass(frozen=True)
class NodeSnapshot:
    node_id: int
    admit: bool
    drain_mode: bool
    reasons: tuple[str, ...]
    inflight_actions: int
    active_sessions: int
    open_circuits: int
    breaker_open_ratio: float
    loop_lag_p95_ms: float
    fd_count: int
    rss_mb: float
    status: str


@dataclass
class QueuedAction:
    tenant_id: str
    workflow_id: str
    policy: SecurityPolicy
    contract: ActionContract
    work_class: WorkClass
    enqueued_ts: float
    result_future: asyncio.Future[ActionExecutionResult]


def _estimate_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if os.uname().sysname.lower().startswith("darwin"):
        return usage / (1024 * 1024)
    return usage / 1024


def _fd_count() -> int:
    for path in ("/dev/fd", "/proc/self/fd"):
        if os.path.exists(path):
            try:
                return len(os.listdir(path))
            except OSError:
                continue
    return -1


def _p95(values: deque[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * 0.95))
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


def classify_work_class(contract: ActionContract) -> WorkClass:
    if isinstance(contract.metadata, dict):
        explicit = contract.metadata.get("work_class")
        if explicit in (WorkClass.LIGHT.value, WorkClass.HEAVY.value):
            return WorkClass(explicit)

    action = contract.action_spec.action_type
    if action in (ActionType.UPLOAD, ActionType.DOWNLOAD_TRIGGER, ActionType.CUSTOM_JS_RESTRICTED, ActionType.NAVIGATE):
        return WorkClass.HEAVY
    return WorkClass.LIGHT


class ExecutionNode(Protocol):
    node_id: int

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    def can_admit(self) -> bool: ...

    def admission_limit(self) -> int: ...

    def snapshot(self) -> NodeSnapshot: ...

    async def execute_contract(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
        contract: ActionContract,
    ) -> ActionExecutionResult: ...

    async def close_workflow_session(self, workflow_id: str) -> None: ...

    async def verify_audit_chain(self, tenant_id: str, workflow_id: str) -> tuple[bool, str]: ...

    async def get_replay_trace(self, tenant_id: str, workflow_id: str) -> list[dict[str, Any]]: ...

    async def get_structured_state(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
    ) -> dict[str, Any]: ...

    async def open_tab(self, tenant_id: str, workflow_id: str, policy: SecurityPolicy, url: str) -> str: ...

    async def switch_tab(self, workflow_id: str, tab_id: str) -> None: ...

    async def list_tabs(self, workflow_id: str) -> list[dict[str, Any]]: ...

    async def register_upload_artifact(
        self,
        tenant_id: str,
        workflow_id: str,
        action_id: str,
        source_path: str,
    ) -> dict[str, Any]: ...

    def set_tenant_quota(self, tenant_id: str, quota: TenantQuota) -> None: ...


class EngineExecutionNode:
    def __init__(
        self,
        node_id: int,
        engine: PredatorEngineV2,
        slo: NodeAdmissionSLO,
        monitor_interval_ms: int = 250,
    ) -> None:
        self.node_id = node_id
        self._engine = engine
        self._slo = slo
        self._monitor_interval_ms = max(50, monitor_interval_ms)
        self._monitor_task: asyncio.Task[None] | None = None
        self._lag_samples: deque[float] = deque(maxlen=80)
        self._inflight = 0
        self._snapshot = NodeSnapshot(
            node_id=node_id,
            admit=True,
            drain_mode=False,
            reasons=(),
            inflight_actions=0,
            active_sessions=0,
            open_circuits=0,
            breaker_open_ratio=0.0,
            loop_lag_p95_ms=0.0,
            fd_count=_fd_count(),
            rss_mb=_estimate_rss_mb(),
            status="initializing",
        )

    async def initialize(self) -> None:
        await self._engine.initialize()
        self._monitor_task = asyncio.create_task(self._monitor())

    async def close(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        await self._engine.close()

    def _update_snapshot(self) -> None:
        health = self._engine.get_health()
        active_sessions = int(health.get("active_sessions", 0))
        open_circuits = int(health.get("open_circuits", 0))
        details = health.get("details", {})
        circuits = details.get("circuits", {}) if isinstance(details, dict) else {}
        total_circuits = len(circuits) if isinstance(circuits, dict) else 0
        breaker_ratio = (open_circuits / total_circuits) if total_circuits > 0 else 0.0
        lag_p95 = _p95(self._lag_samples)
        fd_count = _fd_count()
        rss_mb = _estimate_rss_mb()

        reasons: list[str] = []
        if self._inflight >= self._slo.max_inflight_actions:
            reasons.append("inflight_limit")
        if active_sessions > self._slo.max_active_sessions:
            reasons.append("active_sessions")
        if lag_p95 > self._slo.max_loop_lag_p95_ms:
            reasons.append("loop_lag")
        if fd_count >= 0 and fd_count > self._slo.max_fd_count:
            reasons.append("fd_count")
        if rss_mb > self._slo.max_rss_mb:
            reasons.append("rss_mb")
        if breaker_ratio > self._slo.max_breaker_open_ratio:
            reasons.append("breaker_open_ratio")

        drain_mode = bool(reasons)
        self._snapshot = NodeSnapshot(
            node_id=self.node_id,
            admit=not drain_mode,
            drain_mode=drain_mode,
            reasons=tuple(reasons),
            inflight_actions=self._inflight,
            active_sessions=active_sessions,
            open_circuits=open_circuits,
            breaker_open_ratio=breaker_ratio,
            loop_lag_p95_ms=lag_p95,
            fd_count=fd_count,
            rss_mb=rss_mb,
            status=str(health.get("status", "unknown")),
        )

    async def _monitor(self) -> None:
        loop = asyncio.get_running_loop()
        interval = self._monitor_interval_ms / 1000.0
        next_tick = loop.time() + interval
        while True:
            await asyncio.sleep(interval)
            now = loop.time()
            lag_ms = max(0.0, (now - next_tick) * 1000.0)
            next_tick = now + interval
            self._lag_samples.append(lag_ms)
            self._update_snapshot()

    def can_admit(self) -> bool:
        return self._snapshot.admit

    def admission_limit(self) -> int:
        return max(1, self._slo.max_inflight_actions)

    def snapshot(self) -> NodeSnapshot:
        return self._snapshot

    async def execute_contract(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
        contract: ActionContract,
    ) -> ActionExecutionResult:
        self._inflight += 1
        try:
            result = await self._engine.execute_contract(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                policy=policy,
                contract=contract,
            )
            return result
        finally:
            self._inflight = max(0, self._inflight - 1)
            self._update_snapshot()

    async def close_workflow_session(self, workflow_id: str) -> None:
        await self._engine.close_workflow_session(workflow_id=workflow_id)

    async def verify_audit_chain(self, tenant_id: str, workflow_id: str) -> tuple[bool, str]:
        return await self._engine.verify_audit_chain(tenant_id=tenant_id, workflow_id=workflow_id)

    async def get_replay_trace(self, tenant_id: str, workflow_id: str) -> list[dict[str, Any]]:
        return await self._engine.get_replay_trace(tenant_id=tenant_id, workflow_id=workflow_id)

    async def get_structured_state(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
    ) -> dict[str, Any]:
        return await self._engine.get_structured_state(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            policy=policy,
        )

    async def open_tab(self, tenant_id: str, workflow_id: str, policy: SecurityPolicy, url: str) -> str:
        return await self._engine.open_tab(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy, url=url)

    async def switch_tab(self, workflow_id: str, tab_id: str) -> None:
        await self._engine.switch_tab(workflow_id=workflow_id, tab_id=tab_id)

    async def list_tabs(self, workflow_id: str) -> list[dict[str, Any]]:
        return await self._engine.list_tabs(workflow_id=workflow_id)

    async def register_upload_artifact(
        self,
        tenant_id: str,
        workflow_id: str,
        action_id: str,
        source_path: str,
    ) -> dict[str, Any]:
        return await self._engine.register_upload_artifact(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            action_id=action_id,
            source_path=source_path,
        )

    def set_tenant_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        self._engine.set_tenant_quota(tenant_id=tenant_id, quota=quota)


class PredatorShardedCluster:
    def __init__(
        self,
        scheduler: ClusterSchedulerConfig | None = None,
        slo: NodeAdmissionSLO | None = None,
        session_config: SessionConfig | None = None,
        artifact_root_dir: str = "/tmp/predator-artifacts",
        audit_root_dir: str = "/tmp/predator-audit",
        control_db_path: str = "/tmp/predator-control-plane/control.db",
        telemetry_dir: str = "/tmp/predator-telemetry",
        nodes: list[ExecutionNode] | None = None,
    ) -> None:
        self._scheduler = scheduler or ClusterSchedulerConfig()
        self._slo = slo or NodeAdmissionSLO()
        self._session_config = session_config or SessionConfig()
        self._artifact_root_dir = artifact_root_dir
        self._audit_root_dir = audit_root_dir
        self._control_db_path = control_db_path
        self._telemetry_dir = telemetry_dir

        self._managed_nodes = nodes is None
        self._nodes: list[ExecutionNode] = nodes or []
        self._node_by_id: dict[int, ExecutionNode] = {}

        self._workflow_affinity: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._dispatch_event = asyncio.Event()
        self._stop_dispatch = asyncio.Event()
        self._dispatch_task: asyncio.Task[None] | None = None

        self._queues: dict[int, dict[WorkClass, dict[str, deque[QueuedAction]]]] = defaultdict(
            lambda: {
                WorkClass.LIGHT: defaultdict(deque),
                WorkClass.HEAVY: defaultdict(deque),
            }
        )
        self._tenant_rr: dict[int, dict[WorkClass, deque[str]]] = defaultdict(
            lambda: {
                WorkClass.LIGHT: deque(),
                WorkClass.HEAVY: deque(),
            }
        )
        self._class_cycle: tuple[WorkClass, ...] = (
            tuple([WorkClass.LIGHT] * max(1, self._scheduler.light_weight))
            + tuple([WorkClass.HEAVY] * max(1, self._scheduler.heavy_weight))
        )
        self._class_index: dict[int, int] = defaultdict(int)
        self._reserved_inflight: dict[int, int] = defaultdict(int)

    def _node_count(self) -> int:
        return len(self._nodes)

    def _node_id_for(self, tenant_id: str, workflow_id: str) -> int:
        pinned = self._workflow_affinity.get(workflow_id)
        if pinned is not None:
            return pinned
        digest = sha256(f"{tenant_id}|{workflow_id}".encode("utf-8")).digest()
        node_id = int.from_bytes(digest[:8], "big") % max(1, self._node_count())
        self._workflow_affinity[workflow_id] = node_id
        return node_id

    @staticmethod
    def _node_path(base: str, node_id: int, suffix: str) -> str:
        if base.endswith(".db"):
            root, ext = os.path.splitext(base)
            return f"{root}.node{node_id}{ext}"
        return os.path.join(base, f"node-{node_id}", suffix)

    def _build_nodes(self) -> list[ExecutionNode]:
        nodes: list[ExecutionNode] = []
        count = max(1, self._scheduler.shard_count)
        for node_id in range(count):
            node_session = replace(
                self._session_config,
                max_total_sessions=min(self._session_config.max_total_sessions, self._slo.max_active_sessions),
            )
            engine = PredatorEngineV2(
                session_config=node_session,
                artifact_root_dir=self._node_path(self._artifact_root_dir, node_id, "artifacts"),
                audit_root_dir=self._node_path(self._audit_root_dir, node_id, "audit"),
                control_db_path=self._node_path(self._control_db_path, node_id, "control.db"),
                telemetry_dir=self._node_path(self._telemetry_dir, node_id, "telemetry"),
            )
            node = EngineExecutionNode(
                node_id=node_id,
                engine=engine,
                slo=self._slo,
                monitor_interval_ms=self._scheduler.monitor_interval_ms,
            )
            nodes.append(node)
        return nodes

    async def initialize(self) -> None:
        if not self._nodes:
            self._nodes = self._build_nodes()
        self._node_by_id = {node.node_id: node for node in self._nodes}
        await asyncio.gather(*(node.initialize() for node in self._nodes))
        self._stop_dispatch.clear()
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def close(self) -> None:
        self._stop_dispatch.set()
        self._dispatch_event.set()
        if self._dispatch_task:
            await asyncio.gather(self._dispatch_task, return_exceptions=True)
            self._dispatch_task = None
        await asyncio.gather(*(node.close() for node in self._nodes), return_exceptions=True)

    def _queue_depth(self, node_id: int) -> int:
        total = 0
        for work_class in (WorkClass.LIGHT, WorkClass.HEAVY):
            for queue in self._queues[node_id][work_class].values():
                total += len(queue)
        return total

    def _enqueue(self, node_id: int, item: QueuedAction) -> None:
        tenant_map = self._queues[node_id][item.work_class]
        if item.tenant_id not in tenant_map:
            self._tenant_rr[node_id][item.work_class].append(item.tenant_id)
        tenant_map[item.tenant_id].append(item)
        self._dispatch_event.set()

    def _pop_tenant_rr(self, node_id: int, work_class: WorkClass) -> QueuedAction | None:
        tenant_map = self._queues[node_id][work_class]
        rr = self._tenant_rr[node_id][work_class]
        if not rr:
            return None

        attempts = len(rr)
        for _ in range(attempts):
            tenant_id = rr[0]
            rr.rotate(-1)
            queue = tenant_map.get(tenant_id)
            if not queue:
                try:
                    rr.remove(tenant_id)
                except ValueError:
                    pass
                tenant_map.pop(tenant_id, None)
                continue
            item = queue.popleft()
            if not queue:
                tenant_map.pop(tenant_id, None)
                try:
                    rr.remove(tenant_id)
                except ValueError:
                    pass
            return item
        return None

    def _pop_next(self, node_id: int) -> QueuedAction | None:
        cycle = self._class_cycle
        if not cycle:
            return None

        start = self._class_index[node_id] % len(cycle)
        for offset in range(len(cycle)):
            work_class = cycle[(start + offset) % len(cycle)]
            item = self._pop_tenant_rr(node_id=node_id, work_class=work_class)
            if item is not None:
                self._class_index[node_id] = (start + offset + 1) % len(cycle)
                return item

        fallback = (WorkClass.LIGHT, WorkClass.HEAVY)
        for work_class in fallback:
            item = self._pop_tenant_rr(node_id=node_id, work_class=work_class)
            if item is not None:
                return item
        return None

    async def _run_item(self, node: ExecutionNode, item: QueuedAction) -> None:
        try:
            result = await node.execute_contract(
                tenant_id=item.tenant_id,
                workflow_id=item.workflow_id,
                policy=item.policy,
                contract=item.contract,
            )
        except Exception as exc:
            result = ActionExecutionResult(
                action_id=item.contract.action_id(),
                success=False,
                failure_code="SHARD_NODE_EXECUTION_ERROR",
                verification_passed=False,
                metadata={"exception": str(exc)},
            )
        if not item.result_future.done():
            item.result_future.set_result(result)
        self._reserved_inflight[node.node_id] = max(0, self._reserved_inflight[node.node_id] - 1)
        self._dispatch_event.set()

    async def _dispatch_loop(self) -> None:
        interval = max(0.01, self._scheduler.dispatch_interval_ms / 1000.0)
        while not self._stop_dispatch.is_set():
            dispatched = False
            for node in self._nodes:
                limit = max(1, node.admission_limit())
                while node.can_admit() and self._reserved_inflight[node.node_id] < limit:
                    item = self._pop_next(node.node_id)
                    if item is None:
                        break
                    dispatched = True
                    self._reserved_inflight[node.node_id] += 1
                    asyncio.create_task(self._run_item(node=node, item=item))

            if dispatched:
                continue

            self._dispatch_event.clear()
            try:
                await asyncio.wait_for(self._dispatch_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def execute_contract(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
        contract: ActionContract,
    ) -> ActionExecutionResult:
        if not self._nodes:
            raise RuntimeError("Cluster not initialized")
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[ActionExecutionResult] = loop.create_future()
        async with self._lock:
            node_id = self._node_id_for(tenant_id=tenant_id, workflow_id=workflow_id)
            item = QueuedAction(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                policy=policy,
                contract=contract,
                work_class=classify_work_class(contract),
                enqueued_ts=time.time(),
                result_future=result_future,
            )
            self._enqueue(node_id=node_id, item=item)
        return await result_future

    def _resolve_node(self, tenant_id: str, workflow_id: str) -> ExecutionNode:
        if not self._nodes:
            raise RuntimeError("Cluster not initialized")
        node_id = self._workflow_affinity.get(workflow_id)
        if node_id is None:
            node_id = self._node_id_for(tenant_id=tenant_id, workflow_id=workflow_id)
        return self._node_by_id[node_id]

    async def register_upload_artifact(
        self,
        tenant_id: str,
        workflow_id: str,
        action_id: str,
        source_path: str,
    ) -> dict[str, Any]:
        node = self._resolve_node(tenant_id=tenant_id, workflow_id=workflow_id)
        return await node.register_upload_artifact(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            action_id=action_id,
            source_path=source_path,
        )

    async def verify_audit_chain(self, tenant_id: str, workflow_id: str) -> tuple[bool, str]:
        node = self._resolve_node(tenant_id=tenant_id, workflow_id=workflow_id)
        return await node.verify_audit_chain(tenant_id=tenant_id, workflow_id=workflow_id)

    async def get_replay_trace(self, tenant_id: str, workflow_id: str) -> list[dict[str, Any]]:
        node = self._resolve_node(tenant_id=tenant_id, workflow_id=workflow_id)
        return await node.get_replay_trace(tenant_id=tenant_id, workflow_id=workflow_id)

    async def get_structured_state(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
    ) -> dict[str, Any]:
        node = self._resolve_node(tenant_id=tenant_id, workflow_id=workflow_id)
        return await node.get_structured_state(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            policy=policy,
        )

    async def open_tab(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
        url: str,
    ) -> str:
        node = self._resolve_node(tenant_id=tenant_id, workflow_id=workflow_id)
        return await node.open_tab(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy, url=url)

    async def switch_tab(self, workflow_id: str, tab_id: str) -> None:
        if workflow_id not in self._workflow_affinity:
            raise KeyError(f"No shard affinity for workflow_id={workflow_id}")
        node = self._node_by_id[self._workflow_affinity[workflow_id]]
        await node.switch_tab(workflow_id=workflow_id, tab_id=tab_id)

    async def list_tabs(self, workflow_id: str) -> list[dict[str, Any]]:
        if workflow_id not in self._workflow_affinity:
            return []
        node = self._node_by_id[self._workflow_affinity[workflow_id]]
        return await node.list_tabs(workflow_id=workflow_id)

    async def close_workflow_session(self, workflow_id: str) -> None:
        node_id = self._workflow_affinity.get(workflow_id)
        if node_id is None:
            return
        node = self._node_by_id[node_id]
        await node.close_workflow_session(workflow_id=workflow_id)
        self._workflow_affinity.pop(workflow_id, None)

    def set_tenant_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        for node in self._nodes:
            node.set_tenant_quota(tenant_id=tenant_id, quota=quota)

    def get_health(self) -> dict[str, Any]:
        snapshots = [node.snapshot() for node in self._nodes]
        nodes_payload = [
            {
                "node_id": snap.node_id,
                "admit": snap.admit,
                "drain_mode": snap.drain_mode,
                "reasons": list(snap.reasons),
                "inflight_actions": snap.inflight_actions,
                "active_sessions": snap.active_sessions,
                "open_circuits": snap.open_circuits,
                "breaker_open_ratio": snap.breaker_open_ratio,
                "loop_lag_p95_ms": snap.loop_lag_p95_ms,
                "fd_count": snap.fd_count,
                "rss_mb": snap.rss_mb,
                "status": snap.status,
                "queue_depth": self._queue_depth(snap.node_id),
            }
            for snap in snapshots
        ]
        total_sessions = sum(snap.active_sessions for snap in snapshots)
        total_open_circuits = sum(snap.open_circuits for snap in snapshots)
        total_queue = sum(self._queue_depth(snap.node_id) for snap in snapshots)
        any_drain = any(snap.drain_mode for snap in snapshots)
        status = "healthy" if not any_drain else "degraded"

        return {
            "status": status,
            "cluster": {
                "shard_count": len(snapshots),
                "total_active_sessions": total_sessions,
                "total_open_circuits": total_open_circuits,
                "total_queue_depth": total_queue,
                "workflow_affinity_size": len(self._workflow_affinity),
            },
            "nodes": nodes_payload,
        }
