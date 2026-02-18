from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any
from urllib.parse import urlparse

from app.core.v2.action_contract_validator import ActionContractValidator
from app.core.v2.action_engine import ActionEngine
from app.core.v2.artifact_manager import ArtifactManager
from app.core.v2.audit_trail import AuditTrail
from app.core.v2.control_plane_store import ControlPlaneStore
from app.core.v2.contracts import ActionContract, ActionExecutionResult
from app.core.v2.delta_state_tracker import DeltaStateTracker
from app.core.v2.navigator import Navigator
from app.core.v2.quota_manager import QuotaManager, TenantQuota
from app.core.v2.resilience import DomainCircuitBreaker, HealthMonitor
from app.core.v2.security_layer import SecurityPolicy
from app.core.v2.session_manager import SessionConfig, SessionManager
from app.core.v2.structured_state_extractor import StructuredStateExtractor
from app.core.v2.token_budget import ComponentTokenBudgets, TokenBudgetManager
from app.core.v2.telemetry_sink import JsonlTelemetrySink, TelemetrySink
from app.core.v2.verification_engine import VerificationEngine
from app.core.v2.wait_manager import ChaosPolicy, WaitManager


class PredatorEngineV2:
    """Deterministic browser executor for Temporal Activity boundaries.

    Temporal workflow owns orchestration and retries. This engine executes one
    ActionContract atomically inside an Activity and returns deterministic evidence.
    """

    def __init__(
        self,
        session_config: SessionConfig | None = None,
        artifact_root_dir: str = "/tmp/predator-artifacts",
        audit_root_dir: str = "/tmp/predator-audit",
        control_db_path: str = "/tmp/predator-control-plane/control.db",
        telemetry_dir: str = "/tmp/predator-telemetry",
        default_quota: TenantQuota | None = None,
        telemetry_sink: TelemetrySink | None = None,
        wait_chaos_policy: ChaosPolicy | None = None,
    ) -> None:
        self._control_store = ControlPlaneStore(db_path=control_db_path)
        self._sessions = SessionManager(config=session_config, control_store=self._control_store)
        self._artifacts = ArtifactManager(root_dir=artifact_root_dir)
        self._audit = AuditTrail(root_dir=audit_root_dir)
        self._quota = QuotaManager(default_quota=default_quota, store=self._control_store)
        self._breaker = DomainCircuitBreaker(store=self._control_store)
        self._health = HealthMonitor()
        self._budget = TokenBudgetManager()
        self._validator = ActionContractValidator()
        self._wait_chaos_policy = wait_chaos_policy
        self._telemetry_sink = telemetry_sink or JsonlTelemetrySink(root_dir=telemetry_dir)
        self._ledger: dict[str, ActionExecutionResult] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._sessions.initialize()

    async def close(self) -> None:
        await self._sessions.close()

    def set_tenant_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        self._quota.set_quota(tenant_id, quota)

    def _domain_from_url(self, url: str) -> str:
        return urlparse(url).netloc.lower()

    async def _audit_and_cache(
        self,
        tenant_id: str,
        workflow_id: str,
        action_id: str,
        canonical_contract_json: str,
        result: ActionExecutionResult,
    ) -> ActionExecutionResult:
        async with self._lock:
            self._ledger[action_id] = result

        await self._audit.append(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            action_id=action_id,
            canonical_contract_json=canonical_contract_json,
            result=result.to_dict(),
        )
        await self._telemetry_sink.emit(
            {
                "event": "action_result",
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
                "action_id": action_id,
                "success": result.success,
                "failure_code": result.failure_code,
                "telemetry": result.telemetry,
                "metadata": result.metadata,
            }
        )
        return result

    async def register_upload_artifact(
        self,
        tenant_id: str,
        workflow_id: str,
        action_id: str,
        source_path: str,
    ) -> dict[str, Any]:
        record = self._artifacts.register_existing_upload(
            workflow_id=workflow_id,
            action_id=action_id,
            source_path=source_path,
        )
        artifact_decision = self._quota.check_artifact_quota(tenant_id=tenant_id, additional_bytes=record.size)
        if not artifact_decision.allowed:
            raise RuntimeError(artifact_decision.code)
        self._quota.register_artifact_bytes(tenant_id=tenant_id, size_bytes=record.size)
        return asdict(record)

    async def execute_contract(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
        contract: ActionContract,
    ) -> ActionExecutionResult:
        action_id = contract.action_id()
        canonical_contract_json = contract.canonical_json()
        tenant_quota = self._quota.quota_for(tenant_id)

        async with self._lock:
            cached = self._ledger.get(action_id)
            if cached is not None:
                return cached

        # Cross-process safe idempotency fallback from immutable audit records.
        existing = await self._audit.get_record_by_action(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            action_id=action_id,
        )
        if existing is not None:
            restored = ActionExecutionResult(
                action_id=existing.action_id,
                success=existing.success,
                failure_code=existing.failure_code,
                attempts=1,
                verification_passed=existing.success,
                pre_state_id=existing.pre_state_id,
                post_state_id=existing.post_state_id,
                state_delta=existing.state_delta,
                network_summary=existing.network_summary,
                telemetry=existing.telemetry,
                artifacts=existing.artifacts,
                metadata=existing.metadata,
            )
            async with self._lock:
                self._ledger[action_id] = restored
            return restored

        validation_decision = self._validator.validate(contract)
        if not validation_decision.allowed:
            return await self._audit_and_cache(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                action_id=action_id,
                canonical_contract_json=canonical_contract_json,
                result=ActionExecutionResult(
                    action_id=action_id,
                    success=False,
                    failure_code=validation_decision.code,
                    metadata={"detail": validation_decision.detail},
                ),
            )

        if not self._sessions.has_session(workflow_id):
            session_decision = self._quota.check_session_quota(
                tenant_id=tenant_id,
                active_sessions=self._sessions.active_session_count_for_tenant(tenant_id),
            )
            if not session_decision.allowed:
                return await self._audit_and_cache(
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    action_id=action_id,
                    canonical_contract_json=canonical_contract_json,
                    result=ActionExecutionResult(
                        action_id=action_id,
                        success=False,
                        failure_code=session_decision.code,
                        metadata={"detail": session_decision.detail},
                    ),
                )

        rate_decision = self._quota.check_action_rate(tenant_id=tenant_id)
        if not rate_decision.allowed:
            return await self._audit_and_cache(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                action_id=action_id,
                canonical_contract_json=canonical_contract_json,
                result=ActionExecutionResult(
                    action_id=action_id,
                    success=False,
                    failure_code=rate_decision.code,
                    metadata={"detail": rate_decision.detail},
                ),
            )
        self._quota.register_action(tenant_id=tenant_id)

        try:
            session = await self._sessions.get_or_create_session(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                policy=policy,
            )
        except RuntimeError as exc:
            failure_code = str(exc) or "SESSION_INIT_FAILED"
            return await self._audit_and_cache(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                action_id=action_id,
                canonical_contract_json=canonical_contract_json,
                result=ActionExecutionResult(
                    action_id=action_id,
                    success=False,
                    failure_code=failure_code,
                    metadata={"detail": "session allocation failed"},
                ),
            )

        # Security gate before action execution.
        current_url = session.page.url or "about:blank"
        navigation_target = contract.action_spec.url
        if navigation_target:
            nav_decision = session.security_layer.evaluate_navigation(navigation_target)
            if not nav_decision.allowed:
                return await self._audit_and_cache(
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    action_id=action_id,
                    canonical_contract_json=canonical_contract_json,
                    result=ActionExecutionResult(
                        action_id=action_id,
                        success=False,
                        failure_code=nav_decision.code,
                        metadata={"detail": nav_decision.detail},
                    ),
                )

        action_domain = self._domain_from_url(navigation_target or current_url)
        if action_domain:
            circuit_decision = self._breaker.allow(action_domain, tenant_id=tenant_id)
            if not circuit_decision.allowed:
                return await self._audit_and_cache(
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    action_id=action_id,
                    canonical_contract_json=canonical_contract_json,
                    result=ActionExecutionResult(
                        action_id=action_id,
                        success=False,
                        failure_code=circuit_decision.code,
                        metadata={"detail": circuit_decision.detail},
                    ),
                )

        action_decision = session.security_layer.evaluate_action(
            contract.action_spec.action_type,
            current_url,
            metadata=contract.metadata,
        )
        if not action_decision.allowed:
            return await self._audit_and_cache(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                action_id=action_id,
                canonical_contract_json=canonical_contract_json,
                result=ActionExecutionResult(
                    action_id=action_id,
                    success=False,
                    failure_code=action_decision.code,
                    metadata={"detail": action_decision.detail},
                ),
            )

        wait_manager = WaitManager(session.page, chaos_policy=self._wait_chaos_policy)
        navigator = Navigator(session.page)
        extractor = StructuredStateExtractor(session.page, session.network_observer)
        verifier = VerificationEngine(session.page, session.network_observer)
        delta_tracker = DeltaStateTracker()

        engine = ActionEngine(
            navigator=navigator,
            wait_manager=wait_manager,
            verifier=verifier,
            extractor=extractor,
            delta_tracker=delta_tracker,
            artifacts=self._artifacts,
            runtime_telemetry=session.runtime_telemetry,
        )

        result = await engine.execute(contract=contract, workflow_id=workflow_id)
        if action_domain:
            if result.success:
                self._breaker.record_success(action_domain, tenant_id=tenant_id)
            else:
                self._breaker.record_failure(action_domain, tenant_id=tenant_id)

        payload = result.to_dict()
        component_budgets = ComponentTokenBudgets(
            max_state_delta_tokens=tenant_quota.max_state_delta_tokens,
            max_network_summary_tokens=tenant_quota.max_network_summary_tokens,
            max_metadata_tokens=tenant_quota.max_metadata_tokens,
        )
        budgeted_payload, budget_outcome = self._budget.enforce(
            payload=payload,
            hard_limit_tokens=tenant_quota.max_step_tokens,
            component_budgets=component_budgets,
        )

        if not budget_outcome.allowed:
            budgeted_payload = {
                "action_id": result.action_id,
                "success": False,
                "failure_code": "BUDGET_EXCEEDED",
                "attempts": result.attempts,
                "escalation": result.escalation.value if result.escalation else None,
                "verification_passed": False,
                "pre_state_id": result.pre_state_id,
                "post_state_id": result.post_state_id,
                "state_delta": {},
                "network_summary": {},
                "telemetry": {"budget_tokens": budget_outcome.total_tokens},
                "artifacts": result.artifacts,
                "metadata": {"budget_notes": list(budget_outcome.notes)},
            }

        if isinstance(budgeted_payload.get("metadata"), dict):
            budgeted_payload["metadata"]["budget"] = {
                "tokens": budget_outcome.total_tokens,
                "trimmed": budget_outcome.trimmed,
                "notes": list(budget_outcome.notes),
                "limit": tenant_quota.max_step_tokens,
            }

        result = ActionExecutionResult.from_dict(budgeted_payload)

        if result.artifacts:
            bytes_added = sum(int(item.get("size", 0)) for item in result.artifacts if isinstance(item, dict))
            artifact_decision = self._quota.check_artifact_quota(tenant_id=tenant_id, additional_bytes=bytes_added)
            if artifact_decision.allowed:
                self._quota.register_artifact_bytes(tenant_id=tenant_id, size_bytes=bytes_added)
            else:
                result = ActionExecutionResult(
                    action_id=result.action_id,
                    success=False,
                    failure_code=artifact_decision.code,
                    attempts=result.attempts,
                    escalation=result.escalation,
                    verification_passed=False,
                    pre_state_id=result.pre_state_id,
                    post_state_id=result.post_state_id,
                    state_delta=result.state_delta,
                    network_summary=result.network_summary,
                    telemetry=result.telemetry,
                    artifacts=result.artifacts,
                    metadata={"detail": artifact_decision.detail},
                )

        return await self._audit_and_cache(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            action_id=action_id,
            canonical_contract_json=canonical_contract_json,
            result=result,
        )

    async def verify_audit_chain(self, tenant_id: str, workflow_id: str) -> tuple[bool, str]:
        return await self._audit.verify_chain(tenant_id=tenant_id, workflow_id=workflow_id)

    async def get_replay_trace(self, tenant_id: str, workflow_id: str) -> list[dict[str, Any]]:
        records = await self._audit.list_records(tenant_id=tenant_id, workflow_id=workflow_id)
        return [record.to_dict() for record in records]

    async def open_tab(
        self,
        tenant_id: str,
        workflow_id: str,
        policy: SecurityPolicy,
        url: str,
    ) -> str:
        session = await self._sessions.get_or_create_session(tenant_id=tenant_id, workflow_id=workflow_id, policy=policy)
        nav_decision = session.security_layer.evaluate_navigation(url)
        if not nav_decision.allowed:
            raise RuntimeError(nav_decision.code)

        await session.network_observer.detach()
        await session.runtime_telemetry.detach()
        tab_id = await session.tab_manager.open_tab(url)
        session.page = session.tab_manager.get_page(tab_id)
        await session.network_observer.attach(session.page)
        await session.runtime_telemetry.attach(session.page)
        return tab_id

    async def switch_tab(self, workflow_id: str, tab_id: str) -> None:
        if not self._sessions.has_session(workflow_id):
            raise KeyError(f"No active session for workflow_id={workflow_id}")
        session = self._sessions.get_session(workflow_id)
        await session.network_observer.detach()
        await session.runtime_telemetry.detach()
        session.tab_manager.set_active_tab(tab_id)
        session.page = session.tab_manager.get_page(tab_id)
        await session.network_observer.attach(session.page)
        await session.runtime_telemetry.attach(session.page)

    async def list_tabs(self, workflow_id: str) -> list[dict[str, Any]]:
        if not self._sessions.has_session(workflow_id):
            return []
        session = self._sessions.get_session(workflow_id)
        tabs = await session.tab_manager.list_tabs()
        return [asdict(tab) for tab in tabs]

    async def close_workflow_session(self, workflow_id: str) -> None:
        await self._sessions.close_session(workflow_id)

    def get_health(self) -> dict[str, Any]:
        snapshot = self._breaker.snapshot()
        health = self._health.evaluate(
            active_sessions=self._sessions.total_active_sessions(),
            circuit_snapshot=snapshot,
        )
        return {
            "status": health.status,
            "active_sessions": health.active_sessions,
            "pooled_contexts": self._sessions.pooled_context_count(),
            "open_circuits": health.open_circuits,
            "details": health.details,
        }
