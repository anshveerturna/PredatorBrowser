from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Any


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    UPLOAD = "upload"
    DOWNLOAD_TRIGGER = "download_trigger"
    WAIT_ONLY = "wait_only"
    CUSTOM_JS_RESTRICTED = "custom_js_restricted"


class VerificationRuleType(str, Enum):
    ELEMENT_PRESENT = "element_present"
    TEXT_STATE = "text_state"
    ATTRIBUTE_STATE = "attribute_state"
    NETWORK_STATUS = "network_status"
    JSON_FIELD = "json_field"
    FILE_EXISTS = "file_exists"
    URL_PATTERN = "url_pattern"
    INVARIANT = "invariant"


class EscalationMode(str, Enum):
    RETRY_REBIND = "retry_rebind"
    VISION_FALLBACK = "vision_fallback"
    HUMAN_REVIEW = "human_review"
    FAIL_WORKFLOW = "fail_workflow"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    initial_backoff_ms: int = 250
    max_backoff_ms: int = 2_000
    multiplier: float = 2.0
    retryable_failure_codes: tuple[str, ...] = (
        "ACTION_EXECUTION_FAILED",
        "WAIT_TIMEOUT",
        "TARGET_BIND_FAILED",
    )


@dataclass(frozen=True)
class TimeoutPolicy:
    total_timeout_ms: int = 30_000
    bind_timeout_ms: int = 5_000
    execute_timeout_ms: int = 10_000
    wait_timeout_ms: int = 10_000
    verify_timeout_ms: int = 5_000


@dataclass(frozen=True)
class EscalationPolicy:
    on_exhausted_retries: EscalationMode = EscalationMode.FAIL_WORKFLOW
    on_non_retryable: EscalationMode = EscalationMode.HUMAN_REVIEW


@dataclass(frozen=True)
class ActionSpec:
    action_type: ActionType
    target_eid: str | None = None
    target_fid: str | None = None
    selector: str | None = None
    selector_candidates: tuple[str, ...] = ()
    text: str | None = None
    url: str | None = None
    select_value: str | None = None
    upload_artifact_id: str | None = None
    js_expression: str | None = None
    js_argument: Any = None


@dataclass(frozen=True)
class VerificationRule:
    rule_type: VerificationRuleType
    severity: str = "hard"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WaitCondition:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int | None = None


@dataclass(frozen=True)
class ActionContract:
    workflow_id: str
    run_id: str
    step_index: int
    intent: str
    preconditions: tuple[VerificationRule, ...] = ()
    action_spec: ActionSpec = field(default_factory=lambda: ActionSpec(action_type=ActionType.WAIT_ONLY))
    expected_postconditions: tuple[VerificationRule, ...] = ()
    verification_rules: tuple[VerificationRule, ...] = ()
    wait_conditions: tuple[WaitCondition, ...] = ()
    timeout: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    escalation: EscalationPolicy = field(default_factory=EscalationPolicy)
    metadata: dict[str, Any] = field(default_factory=dict)

    def canonical_dict(self) -> dict[str, Any]:
        raw = asdict(self)

        # Ensure deterministic ordering for map-like fields.
        def normalize(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: normalize(value[k]) for k in sorted(value.keys())}
            if isinstance(value, list):
                return [normalize(v) for v in value]
            return value

        return normalize(raw)

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def action_id(self) -> str:
        digest = sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"act_{digest[:24]}"


@dataclass
class ActionExecutionResult:
    action_id: str
    success: bool
    failure_code: str | None = None
    attempts: int = 1
    escalation: EscalationMode | None = None
    verification_passed: bool = False
    pre_state_id: str | None = None
    post_state_id: str | None = None
    state_delta: dict[str, Any] = field(default_factory=dict)
    network_summary: dict[str, Any] = field(default_factory=dict)
    telemetry: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "success": self.success,
            "failure_code": self.failure_code,
            "attempts": self.attempts,
            "escalation": self.escalation.value if self.escalation else None,
            "verification_passed": self.verification_passed,
            "pre_state_id": self.pre_state_id,
            "post_state_id": self.post_state_id,
            "state_delta": self.state_delta,
            "network_summary": self.network_summary,
            "telemetry": self.telemetry,
            "artifacts": self.artifacts,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ActionExecutionResult":
        escalation_value = payload.get("escalation")
        escalation = EscalationMode(escalation_value) if escalation_value else None
        return cls(
            action_id=payload["action_id"],
            success=bool(payload.get("success", False)),
            failure_code=payload.get("failure_code"),
            attempts=int(payload.get("attempts", 1)),
            escalation=escalation,
            verification_passed=bool(payload.get("verification_passed", False)),
            pre_state_id=payload.get("pre_state_id"),
            post_state_id=payload.get("post_state_id"),
            state_delta=payload.get("state_delta", {}),
            network_summary=payload.get("network_summary", {}),
            telemetry=payload.get("telemetry", {}),
            artifacts=payload.get("artifacts", []),
            metadata=payload.get("metadata", {}),
        )
