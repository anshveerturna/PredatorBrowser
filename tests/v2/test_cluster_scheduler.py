import asyncio
import time
from dataclasses import dataclass, field

import pytest

from app.core.v2.cluster import (
    ClusterSchedulerConfig,
    NodeSnapshot,
    PredatorShardedCluster,
)
from app.core.v2.contracts import ActionContract, ActionExecutionResult, ActionSpec, ActionType
from app.core.v2.quota_manager import TenantQuota
from app.core.v2.security_layer import SecurityPolicy


@dataclass
class FakeNode:
    node_id: int
    max_inflight: int = 1
    delay_seconds: float = 0.01
    admit: bool = True
    inflight: int = 0
    executed_workflows: list[str] = field(default_factory=list)
    executed_tenants: list[str] = field(default_factory=list)

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def can_admit(self) -> bool:
        return self.admit and self.inflight < self.max_inflight

    def admission_limit(self) -> int:
        return max(1, self.max_inflight)

    def snapshot(self) -> NodeSnapshot:
        return NodeSnapshot(
            node_id=self.node_id,
            admit=self.can_admit(),
            drain_mode=not self.can_admit(),
            reasons=(),
            inflight_actions=self.inflight,
            active_sessions=self.inflight,
            open_circuits=0,
            breaker_open_ratio=0.0,
            loop_lag_p95_ms=0.0,
            fd_count=0,
            rss_mb=0.0,
            status="healthy",
        )

    async def execute_contract(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
        contract: ActionContract,
    ) -> ActionExecutionResult:
        self.inflight += 1
        try:
            await asyncio.sleep(self.delay_seconds)
            self.executed_workflows.append(workflow_id)
            self.executed_tenants.append(tenant_id)
            return ActionExecutionResult(
                action_id=contract.action_id(),
                success=True,
                attempts=1,
                verification_passed=True,
            )
        finally:
            self.inflight -= 1

    async def close_workflow_session(self, workflow_id: str) -> None:
        return None

    async def verify_audit_chain(self, tenant_id: str, workflow_id: str) -> tuple[bool, str]:
        return (True, "ok")

    async def get_replay_trace(self, tenant_id: str, workflow_id: str) -> list[dict[str, object]]:
        return []

    async def open_tab(self, tenant_id: str, workflow_id: str, policy: SecurityPolicy, url: str) -> str:
        return "tab-1"

    async def switch_tab(self, workflow_id: str, tab_id: str) -> None:
        return None

    async def list_tabs(self, workflow_id: str) -> list[dict[str, object]]:
        return []

    async def register_upload_artifact(
        self,
        tenant_id: str,
        workflow_id: str,
        action_id: str,
        source_path: str,
    ) -> dict[str, object]:
        return {"artifact_id": "a-1"}

    def set_tenant_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        return None


def _contract(workflow_id: str, run_id: str, step_index: int, action_type: ActionType = ActionType.CLICK) -> ActionContract:
    return ActionContract(
        workflow_id=workflow_id,
        run_id=run_id,
        step_index=step_index,
        intent="test",
        action_spec=ActionSpec(action_type=action_type, selector="#btn"),
    )


@pytest.mark.asyncio
async def test_cluster_shard_routing_is_deterministic() -> None:
    nodes = [FakeNode(node_id=0), FakeNode(node_id=1), FakeNode(node_id=2)]
    cluster = PredatorShardedCluster(
        scheduler=ClusterSchedulerConfig(shard_count=3),
        nodes=nodes,
    )
    await cluster.initialize()
    try:
        first = cluster._node_id_for("tenant-x", "wf-123")
        second = cluster._node_id_for("tenant-x", "wf-123")
        assert first == second
    finally:
        await cluster.close()


@pytest.mark.asyncio
async def test_workflow_affinity_stays_on_same_node() -> None:
    nodes = [FakeNode(node_id=0), FakeNode(node_id=1)]
    cluster = PredatorShardedCluster(
        scheduler=ClusterSchedulerConfig(shard_count=2, dispatch_interval_ms=5),
        nodes=nodes,
    )
    await cluster.initialize()
    policy = SecurityPolicy(allow_domains=("example.com",))
    try:
        await cluster.execute_contract(
            tenant_id="tenant-1",
            workflow_id="wf-affinity",
            policy=policy,
            contract=_contract(workflow_id="wf-affinity", run_id="run-1", step_index=0),
        )
        await cluster.execute_contract(
            tenant_id="tenant-1",
            workflow_id="wf-affinity",
            policy=policy,
            contract=_contract(workflow_id="wf-affinity", run_id="run-2", step_index=1),
        )
    finally:
        await cluster.close()

    executed_counts = [len(node.executed_workflows) for node in nodes]
    assert sorted(executed_counts) == [0, 2]


@pytest.mark.asyncio
async def test_tenant_fairness_round_robin_for_same_work_class() -> None:
    node = FakeNode(node_id=0, max_inflight=1, delay_seconds=0.02)
    cluster = PredatorShardedCluster(
        scheduler=ClusterSchedulerConfig(shard_count=1, dispatch_interval_ms=2, light_weight=1, heavy_weight=1),
        nodes=[node],
    )
    await cluster.initialize()
    policy = SecurityPolicy(allow_domains=("example.com",))

    started = time.perf_counter()
    try:
        tasks = [
            asyncio.create_task(
                cluster.execute_contract(
                    tenant_id="tenant-a",
                    workflow_id="wf-a-1",
                    policy=policy,
                    contract=_contract("wf-a-1", "run-a1", 0),
                )
            ),
            asyncio.create_task(
                cluster.execute_contract(
                    tenant_id="tenant-a",
                    workflow_id="wf-a-2",
                    policy=policy,
                    contract=_contract("wf-a-2", "run-a2", 0),
                )
            ),
            asyncio.create_task(
                cluster.execute_contract(
                    tenant_id="tenant-b",
                    workflow_id="wf-b-1",
                    policy=policy,
                    contract=_contract("wf-b-1", "run-b1", 0),
                )
            ),
            asyncio.create_task(
                cluster.execute_contract(
                    tenant_id="tenant-a",
                    workflow_id="wf-a-3",
                    policy=policy,
                    contract=_contract("wf-a-3", "run-a3", 0),
                )
            ),
        ]
        await asyncio.gather(*tasks)
    finally:
        await cluster.close()

    assert "tenant-b" in node.executed_tenants
    first_b_index = node.executed_tenants.index("tenant-b")
    assert first_b_index <= 2
    assert (time.perf_counter() - started) >= 0.04
