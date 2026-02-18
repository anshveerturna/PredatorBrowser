from app.core.v2.contracts import (
    ActionContract,
    ActionSpec,
    ActionType,
    VerificationRule,
    VerificationRuleType,
)


def test_action_id_is_deterministic_for_same_contract() -> None:
    contract_a = ActionContract(
        workflow_id="wf-1",
        run_id="run-a",
        step_index=1,
        intent="Click submit",
        action_spec=ActionSpec(action_type=ActionType.CLICK, selector="#submit"),
        verification_rules=(
            VerificationRule(
                rule_type=VerificationRuleType.URL_PATTERN,
                payload={"pattern": "done"},
            ),
        ),
        metadata={"b": 2, "a": 1},
    )

    contract_b = ActionContract(
        workflow_id="wf-1",
        run_id="run-a",
        step_index=1,
        intent="Click submit",
        action_spec=ActionSpec(action_type=ActionType.CLICK, selector="#submit"),
        verification_rules=(
            VerificationRule(
                rule_type=VerificationRuleType.URL_PATTERN,
                payload={"pattern": "done"},
            ),
        ),
        metadata={"a": 1, "b": 2},
    )

    assert contract_a.action_id() == contract_b.action_id()


def test_action_id_changes_when_contract_changes() -> None:
    base = ActionContract(
        workflow_id="wf-1",
        run_id="run-a",
        step_index=1,
        intent="Click submit",
        action_spec=ActionSpec(action_type=ActionType.CLICK, selector="#submit"),
    )

    changed = ActionContract(
        workflow_id="wf-1",
        run_id="run-a",
        step_index=2,
        intent="Click submit",
        action_spec=ActionSpec(action_type=ActionType.CLICK, selector="#submit"),
    )

    assert base.action_id() != changed.action_id()
