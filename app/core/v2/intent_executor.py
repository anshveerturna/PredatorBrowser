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
        session = await self._engine._sessions.get_or_create_session(  # noqa: SLF001
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            policy=policy,
        )
        extractor = StructuredStateExtractor(session.page, session.network_observer)
        state = await extractor.extract(prev_state_id=None, downloads=())
        cache_key = WorkflowCacheKey(instruction=intent, start_url=state.url, environment=environment)

        cached_contract = self._cache.get(cache_key, cache_version=self._cache_version)
        if cached_contract is not None:
            contract = ActionContract(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=step_index,
                intent=intent,
                action_spec=ActionSpec(
                    action_type=ActionType(cached_contract["action_spec"]["action_type"]),
                    selector=cached_contract["action_spec"].get("selector"),
                    text=type_text if cached_contract["action_spec"]["action_type"] == ActionType.TYPE.value else None,
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
        contract = self._candidate_to_contract(
            workflow_id=workflow_id,
            run_id=run_id,
            step_index=step_index,
            intent=intent,
            candidate=selected,
            type_text=type_text,
        )
        result = await self._engine.execute_contract(tenant_id, workflow_id, policy, contract)

        if not result.success and len(candidates) > 1:
            for fallback in candidates[1:4]:
                fallback_contract = self._candidate_to_contract(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    step_index=step_index + 1,
                    intent=f"{intent} (fallback)",
                    candidate=fallback,
                    type_text=type_text,
                )
                result = await self._engine.execute_contract(tenant_id, workflow_id, policy, fallback_contract)
                if result.success:
                    selected = fallback
                    break

        if result.success:
            self._cache.put(
                cache_key,
                {
                    "action_spec": {
                        "action_type": contract.action_spec.action_type.value,
                        "selector": selected.selector,
                    }
                },
                cache_version=self._cache_version,
            )

        return {
            "mode": "perception",
            "selected": asdict(selected),
            "candidate_count": len(candidates),
            "cache_key": cache_key.digest(),
            "result": result.to_dict(),
        }
