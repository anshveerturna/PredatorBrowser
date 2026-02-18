from pathlib import Path

import pytest

from app.core.v2.audit_trail import AuditTrail


@pytest.mark.asyncio
async def test_audit_chain_verification(tmp_path: Path) -> None:
    trail = AuditTrail(root_dir=str(tmp_path), signing_key="test-key")

    result_a = {
        "action_id": "act-1",
        "success": True,
        "failure_code": None,
        "pre_state_id": "s-1",
        "post_state_id": "s-2",
        "state_delta": {"changed_sections": ["elements"]},
        "network_summary": {"total_failures": 0},
        "artifacts": [],
        "telemetry": {},
        "metadata": {},
    }

    result_b = {
        "action_id": "act-2",
        "success": False,
        "failure_code": "POSTCONDITION_FAILED",
        "pre_state_id": "s-2",
        "post_state_id": "s-3",
        "state_delta": {"changed_sections": ["errors"]},
        "network_summary": {"total_failures": 1},
        "artifacts": [],
        "telemetry": {},
        "metadata": {},
    }

    await trail.append(
        tenant_id="tenant-a",
        workflow_id="wf-a",
        action_id="act-1",
        canonical_contract_json='{"a":1}',
        result=result_a,
    )
    await trail.append(
        tenant_id="tenant-a",
        workflow_id="wf-a",
        action_id="act-2",
        canonical_contract_json='{"a":2}',
        result=result_b,
    )

    ok, reason = await trail.verify_chain(tenant_id="tenant-a", workflow_id="wf-a")
    assert ok is True
    assert reason == "ok"

    record = await trail.get_record_by_action("tenant-a", "wf-a", "act-2")
    assert record is not None
    assert record.action_id == "act-2"
    assert record.signature
    assert record.contract_json
