from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from app.core.v2.artifact_manager import ArtifactManager, ArtifactRecord
from app.core.v2.contracts import ActionContract, ActionExecutionResult, ActionType
from app.core.v2.delta_state_tracker import DeltaStateTracker
from app.core.v2.navigator import Navigator
from app.core.v2.structured_state_extractor import StructuredStateExtractor
from app.core.v2.telemetry import RuntimeTelemetryBuffer, Telemetry
from app.core.v2.verification_engine import VerificationEngine
from app.core.v2.wait_manager import WaitManager


class ActionEngine:
    def __init__(
        self,
        navigator: Navigator,
        wait_manager: WaitManager,
        verifier: VerificationEngine,
        extractor: StructuredStateExtractor,
        delta_tracker: DeltaStateTracker,
        artifacts: ArtifactManager,
        runtime_telemetry: RuntimeTelemetryBuffer | None = None,
    ) -> None:
        self._navigator = navigator
        self._wait_manager = wait_manager
        self._verifier = verifier
        self._extractor = extractor
        self._delta = delta_tracker
        self._artifacts = artifacts
        self._runtime_telemetry = runtime_telemetry

    async def _dispatch_action(
        self,
        contract: ActionContract,
        state,
        workflow_id: str,
        action_id: str,
    ) -> list[ArtifactRecord]:
        action = contract.action_spec
        artifacts: list[ArtifactRecord] = []

        if action.action_type == ActionType.NAVIGATE:
            if not action.url:
                raise ValueError("NAVIGATE requires url")
            await self._wait_manager.page.goto(
                action.url,
                wait_until="domcontentloaded",
                timeout=contract.timeout.execute_timeout_ms,
            )
            return artifacts

        if action.action_type == ActionType.WAIT_ONLY:
            return artifacts

        if action.action_type == ActionType.CUSTOM_JS_RESTRICTED:
            if not action.js_expression:
                raise ValueError("CUSTOM_JS_RESTRICTED requires js_expression")
            await self._wait_manager.page.evaluate(action.js_expression, action.js_argument)
            return artifacts

        target = self._navigator.bind_target(action, state)
        locator = self._navigator.locator_for_target(target, state)

        if action.action_type == ActionType.CLICK:
            await locator.click(timeout=contract.timeout.execute_timeout_ms)
            return artifacts

        if action.action_type == ActionType.TYPE:
            await locator.fill(action.text or "", timeout=contract.timeout.execute_timeout_ms)
            return artifacts

        if action.action_type == ActionType.SELECT:
            await locator.select_option(value=action.select_value, timeout=contract.timeout.execute_timeout_ms)
            return artifacts

        if action.action_type == ActionType.UPLOAD:
            artifact_id = action.upload_artifact_id
            if not artifact_id:
                raise ValueError("UPLOAD requires upload_artifact_id")
            record = self._artifacts.get_record(artifact_id)
            if not record:
                raise ValueError(f"Unknown upload artifact: {artifact_id}")
            await locator.set_input_files(record.path, timeout=contract.timeout.execute_timeout_ms)
            artifacts.append(record)
            return artifacts

        if action.action_type == ActionType.DOWNLOAD_TRIGGER:
            async with self._artifacts.expect_download(self._wait_manager.page) as dl_info:
                await locator.click(timeout=contract.timeout.execute_timeout_ms)
            download = await dl_info.value
            record = await self._artifacts.save_download(workflow_id=workflow_id, action_id=action_id, download=download)
            artifacts.append(record)
            return artifacts

        raise ValueError(f"Unsupported action type: {action.action_type}")

    async def execute(self, contract: ActionContract, workflow_id: str) -> ActionExecutionResult:
        action_id = contract.action_id()
        telemetry = Telemetry()
        telemetry.event("action_start", {"action_id": action_id, "intent": contract.intent})

        has_post_guard = bool(
            contract.wait_conditions
            or contract.expected_postconditions
            or contract.verification_rules
        )
        if contract.action_spec.action_type != ActionType.WAIT_ONLY and not has_post_guard:
            return ActionExecutionResult(
                action_id=action_id,
                success=False,
                failure_code="MISSING_POST_ACTION_GUARD",
                attempts=1,
                verification_passed=False,
                metadata={
                    "detail": "Non-wait action requires wait_conditions or verification rules",
                },
            )

        previous_state = await self._extractor.extract(prev_state_id=None, downloads=())
        telemetry.event("pre_state_extracted", {"state_id": previous_state.state_id})

        preconditions = await self._verifier.verify(contract.preconditions, previous_state)
        if not preconditions.passed:
            telemetry.event("preconditions_failed", {"count": len(preconditions.failures)})
            return ActionExecutionResult(
                action_id=action_id,
                success=False,
                failure_code="PRECONDITION_FAILED",
                attempts=1,
                verification_passed=False,
                pre_state_id=previous_state.state_id,
                post_state_id=previous_state.state_id,
                telemetry=telemetry.snapshot(),
                metadata={"precondition_failures": [asdict(failure) for failure in preconditions.failures]},
            )

        attempts = 0
        backoff_ms = contract.retry.initial_backoff_ms

        while attempts < contract.retry.max_attempts:
            attempts += 1
            telemetry.event("attempt_start", {"attempt": attempts})

            action_seq = self._extractor.network_sequence
            runtime_seq = self._runtime_telemetry.sequence if self._runtime_telemetry else 0
            try:
                artifacts, wait_outcomes = await self._wait_manager.execute_with_conditions(
                    action=lambda: self._dispatch_action(
                        contract=contract,
                        state=previous_state,
                        workflow_id=workflow_id,
                        action_id=action_id,
                    ),
                    conditions=contract.wait_conditions,
                    mode="all",
                )
                telemetry.event("action_dispatched", {"attempt": attempts})
                telemetry.event(
                    "wait_conditions_satisfied",
                    {"attempt": attempts, "count": len(wait_outcomes)},
                )

                downloads = tuple({"artifact_id": artifact.artifact_id, "path": artifact.path} for artifact in artifacts)
                post_state = await self._extractor.extract(prev_state_id=previous_state.state_id, downloads=downloads)
                telemetry.event("post_state_extracted", {"state_id": post_state.state_id})

                combined_rules = contract.expected_postconditions + contract.verification_rules
                verification = await self._verifier.verify(combined_rules, post_state)

                if verification.passed:
                    delta = self._delta.diff(previous_state, post_state)
                    network_summary = self._extractor.network_summary_since(action_seq)

                    telemetry.event("verification_passed", {"attempt": attempts})
                    runtime_events = (
                        [
                            {
                                "seq": event.seq,
                                "ts": event.ts,
                                "kind": event.kind,
                                "message": event.message,
                            }
                            for event in self._runtime_telemetry.events_since(runtime_seq)
                        ]
                        if self._runtime_telemetry
                        else []
                    )
                    return ActionExecutionResult(
                        action_id=action_id,
                        success=True,
                        attempts=attempts,
                        verification_passed=True,
                        pre_state_id=previous_state.state_id,
                        post_state_id=post_state.state_id,
                        state_delta=delta.to_dict(),
                        network_summary={
                            "total_requests": network_summary.total_requests,
                            "total_responses": network_summary.total_responses,
                            "total_failures": network_summary.total_failures,
                            "failures": [asdict(item) for item in network_summary.failures],
                        },
                        telemetry=telemetry.snapshot(),
                        artifacts=[asdict(artifact) for artifact in artifacts],
                        metadata={
                            "runtime_events": runtime_events,
                            "guard_summary": {
                                "wait_conditions": len(contract.wait_conditions),
                                "verification_rules": len(combined_rules),
                            },
                        },
                    )

                telemetry.event("verification_failed", {"attempt": attempts})
                failure_code = "POSTCONDITION_FAILED"
                retryable = failure_code in contract.retry.retryable_failure_codes
                if not retryable or attempts >= contract.retry.max_attempts:
                    return ActionExecutionResult(
                        action_id=action_id,
                        success=False,
                        failure_code=failure_code,
                        attempts=attempts,
                        escalation=contract.escalation.on_exhausted_retries,
                        verification_passed=False,
                        pre_state_id=previous_state.state_id,
                        post_state_id=post_state.state_id,
                        telemetry=telemetry.snapshot(),
                        metadata={"verification_failures": [asdict(item) for item in verification.failures]},
                    )

            except Exception as exc:
                failure_code = "ACTION_EXECUTION_FAILED"
                telemetry.event("attempt_error", {"attempt": attempts, "error": str(exc), "failure_code": failure_code})

                retryable = failure_code in contract.retry.retryable_failure_codes
                if not retryable or attempts >= contract.retry.max_attempts:
                    return ActionExecutionResult(
                        action_id=action_id,
                        success=False,
                        failure_code=failure_code,
                        attempts=attempts,
                        escalation=contract.escalation.on_exhausted_retries,
                        verification_passed=False,
                        pre_state_id=previous_state.state_id,
                        post_state_id=previous_state.state_id,
                        telemetry=telemetry.snapshot(),
                        metadata={"exception": str(exc)},
                    )

            await asyncio.sleep(backoff_ms / 1000)
            backoff_ms = min(int(backoff_ms * contract.retry.multiplier), contract.retry.max_backoff_ms)

        return ActionExecutionResult(
            action_id=action_id,
            success=False,
            failure_code="RETRY_EXHAUSTED",
            attempts=attempts,
            escalation=contract.escalation.on_exhausted_retries,
            verification_passed=False,
            pre_state_id=previous_state.state_id,
            post_state_id=previous_state.state_id,
            telemetry=telemetry.snapshot(),
        )
