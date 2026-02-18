from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.core.v2.state_models import StructuredState, StateDelta, estimate_tokens


class DeltaStateTracker:
    def __init__(self, max_ops_per_section: int = 24) -> None:
        self._max_ops = max_ops_per_section

    def _map_by_id(self, items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
        mapped: dict[str, dict[str, Any]] = {}
        for item in items:
            item_id = str(item[key])
            mapped[item_id] = item
        return mapped

    def _diff_collection(self, prev: list[dict[str, Any]], new: list[dict[str, Any]], key: str) -> tuple[dict[str, Any], ...]:
        prev_map = self._map_by_id(prev, key)
        new_map = self._map_by_id(new, key)

        ops: list[dict[str, Any]] = []

        for item_id in sorted(new_map.keys() - prev_map.keys()):
            ops.append({"op": "add", "id": item_id, "value": new_map[item_id]})
            if len(ops) >= self._max_ops:
                return tuple(ops)

        for item_id in sorted(prev_map.keys() - new_map.keys()):
            ops.append({"op": "remove", "id": item_id})
            if len(ops) >= self._max_ops:
                return tuple(ops)

        for item_id in sorted(prev_map.keys() & new_map.keys()):
            if prev_map[item_id] == new_map[item_id]:
                continue
            changed_fields = {
                field: new_map[item_id][field]
                for field in new_map[item_id].keys()
                if prev_map[item_id].get(field) != new_map[item_id].get(field)
            }
            ops.append({"op": "update", "id": item_id, "changes": changed_fields})
            if len(ops) >= self._max_ops:
                return tuple(ops)

        return tuple(ops)

    def diff(self, previous: StructuredState | None, current: StructuredState) -> StateDelta:
        if previous is None:
            baseline = current.to_model_dict()
            token_estimate = estimate_tokens(baseline)
            return StateDelta(
                prev_state_id=None,
                new_state_id=current.state_id,
                changed_sections=("full_state",),
                section_hash_changes={"full_state": ("", current.state_hashes.get("url", ""))},
                element_ops=(
                    {
                        "op": "replace",
                        "count": len(current.interactive_elements),
                        "items": baseline["interactive_elements"][: self._max_ops],
                    },
                ),
                form_ops=(
                    {
                        "op": "replace",
                        "count": len(current.forms),
                        "items": baseline["forms"][: self._max_ops],
                    },
                ),
                error_ops=(
                    {
                        "op": "replace",
                        "count": len(current.visible_errors),
                        "items": baseline["visible_errors"][: self._max_ops],
                    },
                ),
                network_delta=baseline["network_summary"],
                token_estimate=token_estimate,
            )

        prev_model = previous.to_model_dict()
        new_model = current.to_model_dict()

        changed_sections: list[str] = []
        hash_changes: dict[str, tuple[str, str]] = {}

        for key, new_hash in current.state_hashes.items():
            prev_hash = previous.state_hashes.get(key, "")
            if prev_hash != new_hash:
                changed_sections.append(key)
                hash_changes[key] = (prev_hash, new_hash)

        element_ops: tuple[dict[str, Any], ...] = ()
        form_ops: tuple[dict[str, Any], ...] = ()
        error_ops: tuple[dict[str, Any], ...] = ()

        if "elements" in changed_sections:
            element_ops = self._diff_collection(prev_model["interactive_elements"], new_model["interactive_elements"], "eid")

        if "forms" in changed_sections:
            form_ops = self._diff_collection(prev_model["forms"], new_model["forms"], "form_id")

        if "errors" in changed_sections:
            error_ops = self._diff_collection(prev_model["visible_errors"], new_model["visible_errors"], "error_id")

        network_delta: dict[str, Any] = {}
        if "network" in changed_sections:
            network_delta = new_model["network_summary"]

        payload = {
            "changed_sections": changed_sections,
            "section_hash_changes": hash_changes,
            "element_ops": element_ops,
            "form_ops": form_ops,
            "error_ops": error_ops,
            "network_delta": network_delta,
        }

        return StateDelta(
            prev_state_id=previous.state_id,
            new_state_id=current.state_id,
            changed_sections=tuple(changed_sections),
            section_hash_changes=hash_changes,
            element_ops=element_ops,
            form_ops=form_ops,
            error_ops=error_ops,
            network_delta=network_delta,
            token_estimate=estimate_tokens(payload),
        )
