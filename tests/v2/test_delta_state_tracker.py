from app.core.v2.delta_state_tracker import DeltaStateTracker
from app.core.v2.state_models import (
    InteractiveElementState,
    NetworkSummaryState,
    StructuredState,
)


def make_state(state_id: str, element_name: str) -> StructuredState:
    element = InteractiveElementState(
        eid="e-1",
        fid="f-1",
        role="button",
        name_short=element_name,
        element_type="button",
        enabled=True,
        visible=True,
        required=False,
        checked=None,
        value_hint=None,
        bbox_norm=(0.0, 0.0, 0.1, 0.1),
        selector_hint_id="sh-1",
        stability_score=0.8,
        selector_hints=("#submit",),
    )

    return StructuredState(
        state_id=state_id,
        prev_state_id=None,
        url="https://example.com",
        page_phase="complete",
        frame_summary=(),
        interactive_elements=(element,),
        forms=(),
        visible_errors=(),
        network_summary=NetworkSummaryState(
            total_requests=0,
            total_responses=0,
            total_failures=0,
            failures=(),
        ),
        downloads=(),
        state_hashes={
            "frames": "0",
            "elements": element_name,
            "forms": "0",
            "errors": "0",
            "network": "0",
            "downloads": "0",
            "url": "0",
        },
        budget_stats={"estimated_tokens": 20},
    )


def test_delta_for_initial_state_replaces_sections() -> None:
    tracker = DeltaStateTracker()
    current = make_state("s-1", "Submit")

    delta = tracker.diff(None, current)

    assert delta.prev_state_id is None
    assert delta.new_state_id == "s-1"
    assert "full_state" in delta.changed_sections
    assert delta.token_estimate > 0


def test_delta_for_updated_element_is_update_op() -> None:
    tracker = DeltaStateTracker()
    previous = make_state("s-1", "Submit")
    current = make_state("s-2", "Continue")

    delta = tracker.diff(previous, current)

    assert "elements" in delta.changed_sections
    assert any(op["op"] == "update" for op in delta.element_ops)
