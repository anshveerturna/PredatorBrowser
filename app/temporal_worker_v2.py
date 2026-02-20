"""Temporal worker wiring for Predator v2.

This module provides a deployable worker entrypoint when `temporalio` is available.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from app.core.v2 import (
    ClusterSchedulerConfig,
    NodeAdmissionSLO,
    PredatorEngineV2,
    PredatorShardedCluster,
    SecurityPolicy,
)
from app.core.v2.contracts import (
    ActionContract,
    ActionSpec,
    ActionType,
    EscalationPolicy,
    RetryPolicy,
    TimeoutPolicy,
    VerificationRule,
    VerificationRuleType,
    WaitCondition,
)
from app.core.v2.temporal_activity import ActivityRequest, PredatorTemporalActivity

try:
    from temporalio.client import Client
    from temporalio.worker import Worker
except Exception:  # pragma: no cover - optional dependency path
    Client = None  # type: ignore[assignment]
    Worker = None  # type: ignore[assignment]


@dataclass(frozen=True)
class WorkerConfig:
    temporal_address: str
    task_queue: str


class PredatorV2Activities:
    def __init__(self, adapter: PredatorTemporalActivity) -> None:
        self._adapter = adapter

    def _to_contract(self, payload: dict[str, Any]) -> ActionContract:
        return ActionContract(
            workflow_id=payload["workflow_id"],
            run_id=payload["run_id"],
            step_index=int(payload["step_index"]),
            intent=payload["intent"],
            preconditions=tuple(
                VerificationRule(
                    rule_type=VerificationRuleType(item["rule_type"]),
                    severity=item.get("severity", "hard"),
                    payload=item.get("payload", {}),
                )
                for item in payload.get("preconditions", [])
            ),
            action_spec=ActionSpec(
                action_type=ActionType(payload["action_spec"]["action_type"]),
                target_eid=payload["action_spec"].get("target_eid"),
                target_fid=payload["action_spec"].get("target_fid"),
                selector=payload["action_spec"].get("selector"),
                selector_candidates=tuple(payload["action_spec"].get("selector_candidates", [])),
                text=payload["action_spec"].get("text"),
                url=payload["action_spec"].get("url"),
                select_value=payload["action_spec"].get("select_value"),
                upload_artifact_id=payload["action_spec"].get("upload_artifact_id"),
                js_expression=payload["action_spec"].get("js_expression"),
                js_argument=payload["action_spec"].get("js_argument"),
            ),
            expected_postconditions=tuple(
                VerificationRule(
                    rule_type=VerificationRuleType(item["rule_type"]),
                    severity=item.get("severity", "hard"),
                    payload=item.get("payload", {}),
                )
                for item in payload.get("expected_postconditions", [])
            ),
            verification_rules=tuple(
                VerificationRule(
                    rule_type=VerificationRuleType(item["rule_type"]),
                    severity=item.get("severity", "hard"),
                    payload=item.get("payload", {}),
                )
                for item in payload.get("verification_rules", [])
            ),
            wait_conditions=tuple(
                WaitCondition(
                    kind=item["kind"],
                    payload=item.get("payload", {}),
                    timeout_ms=item.get("timeout_ms"),
                )
                for item in payload.get("wait_conditions", [])
            ),
            timeout=TimeoutPolicy(**payload.get("timeout", {})),
            retry=RetryPolicy(**payload.get("retry", {})),
            escalation=EscalationPolicy(**payload.get("escalation", {})),
            metadata=payload.get("metadata", {}),
        )

    async def execute_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = ActivityRequest(
            tenant_id=payload["tenant_id"],
            workflow_id=payload["workflow_id"],
            security_policy=SecurityPolicy(**payload["security_policy"]),
            contract=self._to_contract(payload["contract"]),
        )
        return await self._adapter.execute_action(request)

    async def verify_audit_chain(self, tenant_id: str, workflow_id: str) -> dict[str, Any]:
        return await self._adapter.verify_audit_chain(tenant_id=tenant_id, workflow_id=workflow_id)

    async def get_replay_trace(self, tenant_id: str, workflow_id: str) -> list[dict[str, Any]]:
        return await self._adapter.get_replay_trace(tenant_id=tenant_id, workflow_id=workflow_id)


async def run_worker(config: WorkerConfig) -> None:
    if Client is None or Worker is None:
        raise RuntimeError("temporalio is not installed. Install temporalio to run Predator v2 worker.")

    shard_count = int(os.getenv("PREDATOR_V2_SHARDS", "1"))
    if shard_count > 1:
        scheduler = ClusterSchedulerConfig(
            shard_count=shard_count,
            dispatch_interval_ms=int(os.getenv("PREDATOR_V2_DISPATCH_INTERVAL_MS", "20")),
            monitor_interval_ms=int(os.getenv("PREDATOR_V2_MONITOR_INTERVAL_MS", "250")),
            light_weight=int(os.getenv("PREDATOR_V2_LIGHT_WEIGHT", "3")),
            heavy_weight=int(os.getenv("PREDATOR_V2_HEAVY_WEIGHT", "1")),
        )
        slo = NodeAdmissionSLO(
            max_active_sessions=int(os.getenv("PREDATOR_V2_SLO_MAX_ACTIVE_SESSIONS", "120")),
            max_inflight_actions=int(os.getenv("PREDATOR_V2_SLO_MAX_INFLIGHT_ACTIONS", "120")),
            max_loop_lag_p95_ms=float(os.getenv("PREDATOR_V2_SLO_MAX_LOOP_LAG_MS", "1200")),
            max_fd_count=int(os.getenv("PREDATOR_V2_SLO_MAX_FD", "1024")),
            max_rss_mb=float(os.getenv("PREDATOR_V2_SLO_MAX_RSS_MB", "1024")),
            max_breaker_open_ratio=float(os.getenv("PREDATOR_V2_SLO_MAX_BREAKER_RATIO", "0.5")),
        )
        engine: PredatorEngineV2 | PredatorShardedCluster = PredatorShardedCluster(
            scheduler=scheduler,
            slo=slo,
            audit_root_dir=os.getenv("PREDATOR_V2_AUDIT_DIR", "/tmp/predator-audit"),
            artifact_root_dir=os.getenv("PREDATOR_V2_ARTIFACT_DIR", "/tmp/predator-artifacts"),
            control_db_path=os.getenv("PREDATOR_V2_CONTROL_DB", "/tmp/predator-control-plane/control.db"),
            telemetry_dir=os.getenv("PREDATOR_V2_TELEMETRY_DIR", "/tmp/predator-telemetry"),
        )
    else:
        engine = PredatorEngineV2(
            audit_root_dir=os.getenv("PREDATOR_V2_AUDIT_DIR", "/tmp/predator-audit"),
            artifact_root_dir=os.getenv("PREDATOR_V2_ARTIFACT_DIR", "/tmp/predator-artifacts"),
            control_db_path=os.getenv("PREDATOR_V2_CONTROL_DB", "/tmp/predator-control-plane/control.db"),
            telemetry_dir=os.getenv("PREDATOR_V2_TELEMETRY_DIR", "/tmp/predator-telemetry"),
        )
    await engine.initialize()

    adapter = PredatorTemporalActivity(engine)
    activities = PredatorV2Activities(adapter)

    client = await Client.connect(config.temporal_address)
    worker = Worker(
        client,
        task_queue=config.task_queue,
        activities=[
            activities.execute_action,
            activities.verify_audit_chain,
            activities.get_replay_trace,
        ],
    )

    try:
        await worker.run()
    finally:
        await engine.close()


def run() -> None:
    address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "predator-v2")
    asyncio.run(run_worker(WorkerConfig(temporal_address=address, task_queue=task_queue)))


if __name__ == "__main__":
    run()
