from app.core.v2.perception import LocalPerceptionAdapter
from app.core.v2.state_models import (
    FormState,
    FrameState,
    InteractiveElementState,
    NetworkSummaryState,
    StructuredState,
)


def test_local_observe_ranks_matching_element():
    import asyncio
    adapter = LocalPerceptionAdapter()
    state = StructuredState(
        state_id="s1",
        prev_state_id=None,
        url="https://example.test",
        page_phase="complete",
        frame_summary=(FrameState(fid="f1", parent_fid=None, origin="https://example.test", title_short="", visible=True, interactive_count=2),),
        interactive_elements=(
            InteractiveElementState(
                eid="e1",
                fid="f1",
                role="button",
                name_short="Add to cart",
                element_type="button",
                enabled=True,
                visible=True,
                required=False,
                checked=None,
                value_hint=None,
                bbox_norm=(0.1, 0.1, 0.2, 0.1),
                selector_hint_id="sh1",
                stability_score=0.9,
                selector_hints=("#add-to-cart",),
            ),
            InteractiveElementState(
                eid="e2",
                fid="f1",
                role="link",
                name_short="Contact",
                element_type="a",
                enabled=True,
                visible=True,
                required=False,
                checked=None,
                value_hint=None,
                bbox_norm=(0.1, 0.3, 0.2, 0.1),
                selector_hint_id="sh2",
                stability_score=0.8,
                selector_hints=("a[href='/contact']",),
            ),
        ),
        forms=(FormState(form_id="form1", fid="f1", field_eids=(), required_missing_count=0, submit_eid=None, validation_error_eids=()),),
        visible_errors=(),
        network_summary=NetworkSummaryState(total_requests=0, total_responses=0, total_failures=0, failures=()),
        downloads=(),
        state_hashes={},
        budget_stats={},
    )

    candidates = asyncio.run(adapter.observe(intent="click add to cart", page=None, state=state))

    assert candidates
    assert candidates[0].selector == "#add-to-cart"
