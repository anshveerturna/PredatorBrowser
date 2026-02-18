from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import blake2b
from typing import Any


@dataclass(frozen=True)
class FrameState:
    fid: str
    parent_fid: str | None
    origin: str
    title_short: str
    visible: bool
    interactive_count: int


@dataclass(frozen=True)
class InteractiveElementState:
    eid: str
    fid: str
    role: str
    name_short: str
    element_type: str
    enabled: bool
    visible: bool
    required: bool
    checked: bool | None
    value_hint: str | None
    bbox_norm: tuple[float, float, float, float]
    selector_hint_id: str
    stability_score: float
    selector_hints: tuple[str, ...] = field(default_factory=tuple, repr=False)


@dataclass(frozen=True)
class FormState:
    form_id: str
    fid: str
    field_eids: tuple[str, ...]
    required_missing_count: int
    submit_eid: str | None
    validation_error_eids: tuple[str, ...]


@dataclass(frozen=True)
class VisibleErrorState:
    error_id: str
    fid: str
    kind: str
    text_short: str
    eid: str | None


@dataclass(frozen=True)
class NetworkFailureState:
    route_key: str
    status: int
    status_class: str
    error_signature: str
    latency_ms: int


@dataclass(frozen=True)
class NetworkSummaryState:
    total_requests: int
    total_responses: int
    total_failures: int
    failures: tuple[NetworkFailureState, ...]


@dataclass(frozen=True)
class StructuredState:
    state_id: str
    prev_state_id: str | None
    url: str
    page_phase: str
    frame_summary: tuple[FrameState, ...]
    interactive_elements: tuple[InteractiveElementState, ...]
    forms: tuple[FormState, ...]
    visible_errors: tuple[VisibleErrorState, ...]
    network_summary: NetworkSummaryState
    downloads: tuple[dict[str, Any], ...]
    state_hashes: dict[str, str]
    budget_stats: dict[str, int]

    def to_model_dict(self) -> dict[str, Any]:
        return {
            "state_id": self.state_id,
            "prev_state_id": self.prev_state_id,
            "url": self.url,
            "page_phase": self.page_phase,
            "frame_summary": [f.__dict__ for f in self.frame_summary],
            "interactive_elements": [
                {
                    "eid": e.eid,
                    "fid": e.fid,
                    "role": e.role,
                    "name_short": e.name_short,
                    "type": e.element_type,
                    "enabled": e.enabled,
                    "visible": e.visible,
                    "required": e.required,
                    "checked": e.checked,
                    "value_hint": e.value_hint,
                    "bbox_norm": e.bbox_norm,
                    "selector_hint_id": e.selector_hint_id,
                    "stability_score": e.stability_score,
                }
                for e in self.interactive_elements
            ],
            "forms": [f.__dict__ for f in self.forms],
            "visible_errors": [e.__dict__ for e in self.visible_errors],
            "network_summary": {
                "total_requests": self.network_summary.total_requests,
                "total_responses": self.network_summary.total_responses,
                "total_failures": self.network_summary.total_failures,
                "failures": [f.__dict__ for f in self.network_summary.failures],
            },
            "downloads": list(self.downloads),
            "state_hashes": self.state_hashes,
            "budget_stats": self.budget_stats,
        }


@dataclass(frozen=True)
class StateDelta:
    prev_state_id: str | None
    new_state_id: str
    changed_sections: tuple[str, ...]
    section_hash_changes: dict[str, tuple[str, str]]
    element_ops: tuple[dict[str, Any], ...]
    form_ops: tuple[dict[str, Any], ...]
    error_ops: tuple[dict[str, Any], ...]
    network_delta: dict[str, Any]
    token_estimate: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "prev_state_id": self.prev_state_id,
            "new_state_id": self.new_state_id,
            "changed_sections": list(self.changed_sections),
            "section_hash_changes": self.section_hash_changes,
            "element_ops": list(self.element_ops),
            "form_ops": list(self.form_ops),
            "error_ops": list(self.error_ops),
            "network_delta": self.network_delta,
            "token_estimate": self.token_estimate,
        }


def stable_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return blake2b(blob.encode("utf-8"), digest_size=12).hexdigest()


def estimate_tokens(payload: Any) -> int:
    # Fast and predictable estimator for budget enforcement.
    chars = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
    return max(1, chars // 4)
