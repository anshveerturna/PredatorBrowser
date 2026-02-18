from app.core.v2.state_models import InteractiveElementState, NetworkSummaryState, StructuredState


def test_model_projection_excludes_selector_hints() -> None:
    state = StructuredState(
        state_id="s-1",
        prev_state_id=None,
        url="https://example.com",
        page_phase="complete",
        frame_summary=(),
        interactive_elements=(
            InteractiveElementState(
                eid="e-1",
                fid="f-1",
                role="button",
                name_short="Submit",
                element_type="button",
                enabled=True,
                visible=True,
                required=False,
                checked=None,
                value_hint=None,
                bbox_norm=(0.0, 0.0, 0.1, 0.1),
                selector_hint_id="sh-1",
                stability_score=0.7,
                selector_hints=("#submit", "button[type='submit']"),
            ),
        ),
        forms=(),
        visible_errors=(),
        network_summary=NetworkSummaryState(
            total_requests=0,
            total_responses=0,
            total_failures=0,
            failures=(),
        ),
        downloads=(),
        state_hashes={},
        budget_stats={},
    )

    model = state.to_model_dict()
    element = model["interactive_elements"][0]

    assert "selector_hints" not in element
    assert element["selector_hint_id"] == "sh-1"
