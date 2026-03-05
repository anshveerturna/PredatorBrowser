from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.core.v2.contracts import (
    ActionContract,
    ActionSpec,
    ActionType,
    VerificationRule,
    VerificationRuleType,
)
from app.core.v2.intent_cache import IntentWorkflowCache, WorkflowCacheKey
from app.core.v2.perception import ActionCandidate, PerceptionAdapter
from app.core.v2.security_layer import SecurityPolicy
from app.core.v2.structured_state_extractor import StructuredStateExtractor


class IntentExecutor:
    def __init__(
        self,
        engine: Any,
        perception: PerceptionAdapter,
        cache: IntentWorkflowCache,
        cache_version: int = 1,
    ) -> None:
        self._engine = engine
        self._perception = perception
        self._cache = cache
        self._cache_version = cache_version

    def _candidate_to_contract(
        self,
        workflow_id: str,
        run_id: str,
        step_index: int,
        intent: str,
        candidate: ActionCandidate,
        type_text: str | None,
    ) -> ActionContract:
        action_type = ActionType.TYPE if candidate.method == "type" else ActionType.CLICK
        action_spec = ActionSpec(
            action_type=action_type,
            selector=candidate.selector,
            text=type_text if action_type == ActionType.TYPE else None,
        )
        verification = ()
        if candidate.selector:
            verification = (
                VerificationRule(
                    rule_type=VerificationRuleType.ELEMENT_PRESENT,
                    payload={"selector": candidate.selector},
                ),
            )
        return ActionContract(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=step_index,
            intent=intent,
            action_spec=action_spec,
            verification_rules=verification,
            metadata={
                "candidate": {
                    "description": candidate.description,
                    "confidence": candidate.confidence,
                    "metadata": candidate.metadata,
                }
            },
        )

    async def _build_state(self, tenant_id: str, workflow_id: str, policy: SecurityPolicy) -> Any:
        # Prime workflow session through deterministic engine path first, preserving
        # quota/security/circuit-breaker controls.
        pre_contract = ActionContract(
            workflow_id=workflow_id,
            run_id="intent-bootstrap",
            step_index=0,
            intent="intent bootstrap",
            action_spec=ActionSpec(action_type=ActionType.WAIT_ONLY),
        )
        await self._engine.execute_contract(tenant_id, workflow_id, policy, pre_contract)
        session = self._engine._sessions.get_session(workflow_id)  # noqa: SLF001
        extractor = StructuredStateExtractor(session.page, session.network_observer)
        return session, await extractor.extract(prev_state_id=None, downloads=())

    async def execute_intent(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
        run_id: str,
        step_index: int,
        intent: str,
        type_text: str | None = None,
        environment: str = "default",
    ) -> dict[str, Any]:
        session, state = await self._build_state(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy)
        cache_key = WorkflowCacheKey(instruction=intent, start_url=state.url, environment=environment)

        cached_contract = self._cache.get(cache_key, cache_version=self._cache_version)
        if cached_contract is not None:
            action_type = ActionType(cached_contract["action_spec"]["action_type"])
            contract = ActionContract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=step_index,
                intent=intent,
                action_spec=ActionSpec(
                    action_type=action_type,
                    selector=cached_contract["action_spec"].get("selector"),
                    text=type_text if action_type == ActionType.TYPE else None,
                ),
                metadata={"cache_hit": True, "cache_key": cache_key.digest()},
            )
            result = await self._engine.execute_contract(tenant_id, workflow_id, policy, contract)
            if result.success:
                return {"mode": "cache", "result": result.to_dict(), "cache_key": cache_key.digest()}
            self._cache.invalidate(cache_key)

        candidates = await self._perception.observe(intent=intent, page=session.page, state=state)
        if not candidates:
            return {
                "mode": "exploration",
                "success": False,
                "failure_code": "NO_CANDIDATES",
                "candidates": [],
            }

        selected = candidates[0]
        selected_contract = self._candidate_to_contract(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=step_index,
            intent=intent,
            candidate=selected,
            type_text=type_text,
        )
        result = await self._engine.execute_contract(tenant_id, workflow_id, policy, selected_contract)

        if not result.success and len(candidates) > 1:
            for offset, fallback in enumerate(candidates[1:4], start=1):
                fallback_contract = self._candidate_to_contract(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    step_index=step_index + offset,
                    intent=f"{intent} (fallback)",
                    candidate=fallback,
                    type_text=type_text,
                )
                result = await self._engine.execute_contract(tenant_id, workflow_id, policy, fallback_contract)
                if result.success:
                    selected = fallback
                    selected_contract = fallback_contract
                    break

        if result.success:
            self._cache.put(
                cache_key,
                {
                    "action_spec": {
                        "action_type": selected_contract.action_spec.action_type.value,
                        "selector": selected.selector,
                    }
                },
                cache_version=self._cache_version,
            )

        return {
            "mode": "perception",
            "selected": asdict(selected),
            "candidates": [asdict(item) for item in candidates[:5]],
            "candidate_count": len(candidates),
            "cache_key": cache_key.digest(),
            "result": result.to_dict(),
        }
