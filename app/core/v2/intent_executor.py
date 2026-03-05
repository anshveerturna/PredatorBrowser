from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.core.v2.contracts import (
    ActionContract,
    ActionSpec,
    ActionType,
    VerificationRule,
    VerificationRuleType,
    WaitCondition,
)
from app.core.v2.contracts import EscalationPolicy, RetryPolicy, TimeoutPolicy
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

    def _selector_still_present(self, selector: str | None, state: Any) -> bool:
        if not selector:
            return True
        for element in state.interactive_elements:
            if selector in element.selector_hints:
                return True
        return False

    def _matches_cached_state(self, cached: dict[str, Any], state: Any) -> bool:
        cached_hashes = cached.get("state_hashes", {})
        if not isinstance(cached_hashes, dict):
            return False
        return (
            cached_hashes.get("url") == state.state_hashes.get("url")
            and cached_hashes.get("elements") == state.state_hashes.get("elements")
        )

    def _to_action_spec(self, payload: dict[str, Any]) -> ActionSpec:
        return ActionSpec(
            action_type=ActionType(payload["action_type"]),
            target_eid=payload.get("target_eid"),
            target_fid=payload.get("target_fid"),
            selector=payload.get("selector"),
            selector_candidates=tuple(payload.get("selector_candidates", [])),
            text=payload.get("text"),
            url=payload.get("url"),
            select_value=payload.get("select_value"),
            upload_artifact_id=payload.get("upload_artifact_id"),
            js_expression=payload.get("js_expression"),
            js_argument=payload.get("js_argument"),
        )

    def _to_rules(self, payload: list[dict[str, Any]]) -> tuple[VerificationRule, ...]:
        return tuple(
            VerificationRule(
                rule_type=VerificationRuleType(item["rule_type"]),
                severity=item.get("severity", "hard"),
                payload=item.get("payload", {}),
            )
            for item in payload
        )

    def _to_waits(self, payload: list[dict[str, Any]]) -> tuple[WaitCondition, ...]:
        return tuple(
            WaitCondition(
                kind=item["kind"],
                payload=item.get("payload", {}),
                timeout_ms=item.get("timeout_ms"),
            )
            for item in payload
        )

    def _to_contract(self, payload: dict[str, Any]) -> ActionContract:
        return ActionContract(
            workflow_id=payload["workflow_id"],
            run_id=payload["run_id"],
            step_index=int(payload["step_index"]),
            intent=payload["intent"],
            preconditions=self._to_rules(payload.get("preconditions", [])),
            action_spec=self._to_action_spec(payload["action_spec"]),
            expected_postconditions=self._to_rules(payload.get("expected_postconditions", [])),
            verification_rules=self._to_rules(payload.get("verification_rules", [])),
            wait_conditions=self._to_waits(payload.get("wait_conditions", [])),
            timeout=TimeoutPolicy(**payload.get("timeout", {})),
            retry=RetryPolicy(**payload.get("retry", {})),
            escalation=EscalationPolicy(**payload.get("escalation", {})),
            metadata=payload.get("metadata", {}),
        )

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

        cached_entry = self._cache.get(cache_key, cache_version=self._cache_version)
        if cached_entry is not None:
            contracts_payload = cached_entry.get("contracts", [])
            if not contracts_payload or not self._matches_cached_state(cached_entry, state):
                self._cache.invalidate(cache_key)
            else:
                all_success = True
                replay_result: dict[str, Any] | None = None
                for index, contract_payload in enumerate(contracts_payload):
                    replay_contract = self._to_contract(contract_payload)
                    resolved_text = type_text if replay_contract.action_spec.action_type == ActionType.TYPE else None
                    replay_contract = ActionContract(
                        workflow_id=workflow_id,
                        run_id=run_id,
                        step_index=step_index + index,
                        intent=replay_contract.intent,
                        preconditions=replay_contract.preconditions,
                        action_spec=ActionSpec(
                            action_type=replay_contract.action_spec.action_type,
                            target_eid=replay_contract.action_spec.target_eid,
                            target_fid=replay_contract.action_spec.target_fid,
                            selector=replay_contract.action_spec.selector,
                            selector_candidates=replay_contract.action_spec.selector_candidates,
                            text=resolved_text,
                            url=replay_contract.action_spec.url,
                            select_value=replay_contract.action_spec.select_value,
                            upload_artifact_id=replay_contract.action_spec.upload_artifact_id,
                            js_expression=replay_contract.action_spec.js_expression,
                            js_argument=replay_contract.action_spec.js_argument,
                        ),
                        expected_postconditions=replay_contract.expected_postconditions,
                        verification_rules=replay_contract.verification_rules,
                        wait_conditions=replay_contract.wait_conditions,
                        timeout=replay_contract.timeout,
                        retry=replay_contract.retry,
                        escalation=replay_contract.escalation,
                        metadata={**replay_contract.metadata, "cache_hit": True, "cache_key": cache_key.digest()},
                    )
                    if not self._selector_still_present(replay_contract.action_spec.selector, state):
                        all_success = False
                        break
                    result = await self._engine.execute_contract(tenant_id, workflow_id, policy, replay_contract)
                    replay_result = result.to_dict()
                    if not result.success:
                        all_success = False
                        break
                if all_success and replay_result is not None:
                    return {"mode": "cache", "result": replay_result, "cache_key": cache_key.digest()}
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
                    "contracts": [selected_contract.canonical_dict()],
                    "state_hashes": {
                        "url": state.state_hashes.get("url"),
                        "elements": state.state_hashes.get("elements"),
                    },
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
