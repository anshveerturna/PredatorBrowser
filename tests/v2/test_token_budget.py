from app.core.v2.token_budget import ComponentTokenBudgets, TokenBudgetManager


def test_budget_manager_trims_payload_when_over_limit() -> None:
    manager = TokenBudgetManager(hard_limit_tokens=80)

    payload = {
        "action_id": "act-1",
        "state_delta": {
            "element_ops": [{"op": "update", "id": str(i), "changes": {"name": "x" * 30}} for i in range(30)],
            "form_ops": [],
            "error_ops": [],
        },
        "network_summary": {
            "failures": [{"err": "y" * 30} for _ in range(20)],
        },
        "metadata": {
            "runtime_events": [{"message": "z" * 80} for _ in range(20)],
        },
        "telemetry": {
            "elapsed_ms": 10,
            "counters": {},
            "timeline": [{"phase": "x", "metadata": {"k": "v" * 40}} for _ in range(10)],
        },
    }

    trimmed_payload, outcome = manager.enforce(payload)

    assert outcome.trimmed is True
    assert isinstance(trimmed_payload["metadata"], dict)
    assert outcome.total_tokens <= manager.hard_limit_tokens or outcome.allowed is False


def test_budget_manager_enforces_component_partitions() -> None:
    manager = TokenBudgetManager(hard_limit_tokens=400)
    payload = {
        "state_delta": {
            "from_state_id": "a",
            "to_state_id": "b",
            "changed_sections": ["elements"],
            "section_hashes": {"elements": "h"},
            "element_ops": [{"op": "update", "id": str(i), "changes": {"name": "x" * 40}} for i in range(40)],
            "form_ops": [],
            "error_ops": [],
            "network_delta": {},
        },
        "network_summary": {
            "total_requests": 1,
            "total_responses": 1,
            "total_failures": 20,
            "failures": [{"err": "f" * 80} for _ in range(20)],
        },
        "metadata": {
            "runtime_events": [{"message": "m" * 80} for _ in range(20)],
            "guard_summary": {"wait_conditions": 1, "verification_rules": 1},
        },
    }
    budgets = ComponentTokenBudgets(
        max_state_delta_tokens=80,
        max_network_summary_tokens=40,
        max_metadata_tokens=40,
    )

    trimmed_payload, outcome = manager.enforce(payload=payload, component_budgets=budgets)

    assert outcome.trimmed is True
    assert len(trimmed_payload["state_delta"]["element_ops"]) <= 12
    assert len(trimmed_payload["network_summary"]["failures"]) <= 8
    assert len(trimmed_payload["metadata"].get("runtime_events", [])) <= 10
