from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.v2.contracts import ActionContract
from app.core.v2.security_layer import SecurityPolicy


class ExecutionBackend(Protocol):
    async def execute_contract(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
        contract: ActionContract,
    ) -> Any: ...

    async def get_replay_trace(self, tenant_id: str, workflow_id: str) -> list[dict[str, Any]]: ...

    async def verify_audit_chain(self, tenant_id: str, workflow_id: str) -> tuple[bool, str]: ...

    async def get_structured_state(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
    ) -> dict[str, Any]: ...

    async def list_tabs(self, workflow_id: str) -> list[dict[str, Any]]: ...

    async def open_tab(self, tenant_id: str, workflow_id: str, policy: SecurityPolicy, url: str) -> str: ...

    async def switch_tab(self, workflow_id: str, tab_id: str) -> None: ...

    def get_health(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ActivityRequest:
    tenant_id: str
    workflow_id: str
    security_policy: SecurityPolicy
    contract: ActionContract


class PredatorTemporalActivity:
    """Thin Activity adapter.

    Temporal workflow code should call `execute_action` with a fully specified
    ActionContract. Retries at the workflow layer are safe due to action-id
    idempotency in `PredatorEngineV2`.
    """

    def __init__(self, engine: ExecutionBackend) -> None:
        self._engine = engine

    async def execute_action(self, request: ActivityRequest) -> dict[str, Any]:
        result = await self._engine.execute_contract(
            tenant_id=request.tenant_id,
            workflow_id=request.workflow_id,
            policy=request.security_policy,
            contract=request.contract,
        )
        return result.to_dict()

    async def get_replay_trace(self, tenant_id: str, workflow_id: str) -> list[dict[str, Any]]:
        return await self._engine.get_replay_trace(tenant_id=tenant_id, workflow_id=workflow_id)

    async def verify_audit_chain(self, tenant_id: str, workflow_id: str) -> dict[str, Any]:
        ok, reason = await self._engine.verify_audit_chain(tenant_id=tenant_id, workflow_id=workflow_id)
        return {"ok": ok, "reason": reason}

    async def get_structured_state(
        self,
        tenant_id: str,
        workflow_id: str,
        security_policy: SecurityPolicy,
    ) -> dict[str, Any]:
        return await self._engine.get_structured_state(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            policy=security_policy,
        )

    async def list_tabs(self, workflow_id: str) -> list[dict[str, Any]]:
        return await self._engine.list_tabs(workflow_id=workflow_id)

    async def open_tab(
        self,
        tenant_id: str,
        workflow_id: str,
        security_policy: SecurityPolicy,
        url: str,
    ) -> dict[str, Any]:
        tab_id = await self._engine.open_tab(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            policy=security_policy,
            url=url,
        )
        return {"tab_id": tab_id}

    async def switch_tab(self, workflow_id: str, tab_id: str) -> dict[str, Any]:
        await self._engine.switch_tab(workflow_id=workflow_id, tab_id=tab_id)
        return {"ok": True}

    async def get_health(self) -> dict[str, Any]:
        return self._engine.get_health()
