from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.v2.state_models import estimate_tokens


@dataclass(frozen=True)
class BudgetOutcome:
    allowed: bool
    total_tokens: int
    trimmed: bool
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ComponentTokenBudgets:
    max_state_delta_tokens: int = 500
    max_network_summary_tokens: int = 250
    max_metadata_tokens: int = 250


class TokenBudgetManager:
    def __init__(self, hard_limit_tokens: int = 1_200) -> None:
        self._hard_limit = hard_limit_tokens

    @property
    def hard_limit_tokens(self) -> int:
        return self._hard_limit

    def _trim_runtime_events(self, payload: dict[str, Any], notes: list[str]) -> None:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return
        runtime_events = metadata.get("runtime_events")
        if not isinstance(runtime_events, list):
            return
        if len(runtime_events) <= 10:
            return
        metadata["runtime_events"] = runtime_events[:10]
        notes.append("trimmed_runtime_events_to_10")

    def _trim_runtime_events_to(self, payload: dict[str, Any], cap: int, notes: list[str]) -> None:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return
        runtime_events = metadata.get("runtime_events")
        if not isinstance(runtime_events, list):
            return
        if len(runtime_events) <= cap:
            return
        metadata["runtime_events"] = runtime_events[:cap]
        notes.append(f"trimmed_runtime_events_to_{cap}")

    def _trim_metadata_to_minimal(self, payload: dict[str, Any], notes: list[str]) -> None:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return
        guard_summary = metadata.get("guard_summary")
        payload["metadata"] = {"guard_summary": guard_summary} if isinstance(guard_summary, dict) else {}
        notes.append("compressed_metadata_minimal")

    def _trim_network_failures(self, payload: dict[str, Any], notes: list[str]) -> None:
        network_summary = payload.get("network_summary")
        if not isinstance(network_summary, dict):
            return
        failures = network_summary.get("failures")
        if not isinstance(failures, list):
            return
        if len(failures) <= 8:
            return
        network_summary["failures"] = failures[:8]
        notes.append("trimmed_network_failures_to_8")

    def _trim_network_failures_to(self, payload: dict[str, Any], cap: int, notes: list[str]) -> None:
        network_summary = payload.get("network_summary")
        if not isinstance(network_summary, dict):
            return
        failures = network_summary.get("failures")
        if not isinstance(failures, list):
            return
        if len(failures) <= cap:
            return
        network_summary["failures"] = failures[:cap]
        notes.append(f"trimmed_network_failures_to_{cap}")

    def _trim_network_to_minimal(self, payload: dict[str, Any], notes: list[str]) -> None:
        network_summary = payload.get("network_summary")
        if not isinstance(network_summary, dict):
            return
        payload["network_summary"] = {
            "total_requests": network_summary.get("total_requests", 0),
            "total_responses": network_summary.get("total_responses", 0),
            "total_failures": network_summary.get("total_failures", 0),
            "failures": [],
        }
        notes.append("compressed_network_summary_minimal")

    def _trim_state_delta_ops(self, payload: dict[str, Any], notes: list[str]) -> None:
        state_delta = payload.get("state_delta")
        if not isinstance(state_delta, dict):
            return

        for key in ("element_ops", "form_ops", "error_ops"):
            ops = state_delta.get(key)
            if not isinstance(ops, list):
                continue
            if len(ops) <= 12:
                continue
            state_delta[key] = ops[:12]
            notes.append(f"trimmed_{key}_to_12")

    def _trim_state_delta_ops_to(self, payload: dict[str, Any], cap: int, notes: list[str]) -> None:
        state_delta = payload.get("state_delta")
        if not isinstance(state_delta, dict):
            return

        for key in ("element_ops", "form_ops", "error_ops"):
            ops = state_delta.get(key)
            if not isinstance(ops, list):
                continue
            if len(ops) <= cap:
                continue
            state_delta[key] = ops[:cap]
            notes.append(f"trimmed_{key}_to_{cap}")

    def _trim_state_delta_to_minimal(self, payload: dict[str, Any], notes: list[str]) -> None:
        state_delta = payload.get("state_delta")
        if not isinstance(state_delta, dict):
            return
        payload["state_delta"] = {
            "from_state_id": state_delta.get("from_state_id"),
            "to_state_id": state_delta.get("to_state_id"),
            "changed_sections": state_delta.get("changed_sections", []),
            "section_hashes": state_delta.get("section_hashes", {}),
            "element_ops": [],
            "form_ops": [],
            "error_ops": [],
            "network_delta": {},
        }
        notes.append("compressed_state_delta_minimal")

    def _component_tokens(self, payload: dict[str, Any], key: str) -> int:
        if key not in payload:
            return 0
        return estimate_tokens({key: payload[key]})

    def _enforce_component_budgets(
        self,
        payload: dict[str, Any],
        budgets: ComponentTokenBudgets,
        notes: list[str],
    ) -> None:
        if self._component_tokens(payload, "metadata") > budgets.max_metadata_tokens:
            self._trim_runtime_events(payload, notes)
        if self._component_tokens(payload, "metadata") > budgets.max_metadata_tokens:
            self._trim_runtime_events_to(payload, 5, notes)
        if self._component_tokens(payload, "metadata") > budgets.max_metadata_tokens:
            self._trim_metadata_to_minimal(payload, notes)

        if self._component_tokens(payload, "network_summary") > budgets.max_network_summary_tokens:
            self._trim_network_failures(payload, notes)
        if self._component_tokens(payload, "network_summary") > budgets.max_network_summary_tokens:
            self._trim_network_failures_to(payload, 4, notes)
        if self._component_tokens(payload, "network_summary") > budgets.max_network_summary_tokens:
            self._trim_network_to_minimal(payload, notes)

        if self._component_tokens(payload, "state_delta") > budgets.max_state_delta_tokens:
            self._trim_state_delta_ops(payload, notes)
        if self._component_tokens(payload, "state_delta") > budgets.max_state_delta_tokens:
            self._trim_state_delta_ops_to(payload, 6, notes)
        if self._component_tokens(payload, "state_delta") > budgets.max_state_delta_tokens:
            self._trim_state_delta_to_minimal(payload, notes)

    def enforce(
        self,
        payload: dict[str, Any],
        hard_limit_tokens: int | None = None,
        component_budgets: ComponentTokenBudgets | None = None,
    ) -> tuple[dict[str, Any], BudgetOutcome]:
        limit = hard_limit_tokens if hard_limit_tokens is not None else self._hard_limit
        budgets = component_budgets or ComponentTokenBudgets()
        notes: list[str] = []

        self._enforce_component_budgets(payload=payload, budgets=budgets, notes=notes)

        total = estimate_tokens(payload)
        if total <= limit:
            return payload, BudgetOutcome(
                allowed=True,
                total_tokens=total,
                trimmed=bool(notes),
                notes=tuple(notes),
            )

        # Deterministic trimming order.
        self._trim_runtime_events(payload, notes)
        total = estimate_tokens(payload)
        if total <= limit:
            return payload, BudgetOutcome(allowed=True, total_tokens=total, trimmed=True, notes=tuple(notes))

        self._trim_network_failures(payload, notes)
        total = estimate_tokens(payload)
        if total <= limit:
            return payload, BudgetOutcome(allowed=True, total_tokens=total, trimmed=True, notes=tuple(notes))

        self._trim_state_delta_ops(payload, notes)
        total = estimate_tokens(payload)
        if total <= limit:
            return payload, BudgetOutcome(allowed=True, total_tokens=total, trimmed=True, notes=tuple(notes))

        # Final hard-stop policy: preserve correctness signals and drop heavy optional data.
        if isinstance(payload.get("metadata"), dict):
            payload["metadata"] = {
                "budget_truncated": True,
                "notes": list(notes),
            }
            notes.append("dropped_metadata_payload")

        if isinstance(payload.get("telemetry"), dict):
            telemetry = payload["telemetry"]
            payload["telemetry"] = {
                "elapsed_ms": telemetry.get("elapsed_ms"),
                "counters": telemetry.get("counters", {}),
            }
            notes.append("compressed_telemetry")

        total = estimate_tokens(payload)
        allowed = total <= limit
        return payload, BudgetOutcome(allowed=allowed, total_tokens=total, trimmed=True, notes=tuple(notes))
