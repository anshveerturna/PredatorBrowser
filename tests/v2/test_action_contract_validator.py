from app.core.v2.action_contract_validator import ActionContractValidator
from app.core.v2.contracts import ActionContract, ActionSpec, ActionType


def _base_contract(action_spec: ActionSpec) -> ActionContract:
    return ActionContract(
        workflow_id="wf-1",
        run_id="run-1",
        step_index=0,
        intent="test",
        action_spec=action_spec,
    )


def test_rejects_broad_selector() -> None:
    validator = ActionContractValidator()
    contract = _base_contract(ActionSpec(action_type=ActionType.CLICK, selector="body > *"))

    result = validator.validate(contract)
    assert result.allowed is False
    assert result.code == "INVALID_ACTION_SPEC"


def test_rejects_non_http_navigation_url() -> None:
    validator = ActionContractValidator()
    contract = _base_contract(ActionSpec(action_type=ActionType.NAVIGATE, url="file:///etc/passwd"))

    result = validator.validate(contract)
    assert result.allowed is False
    assert result.code == "INVALID_ACTION_SPEC"


def test_accepts_well_formed_click_contract() -> None:
    validator = ActionContractValidator()
    contract = _base_contract(
        ActionSpec(
            action_type=ActionType.CLICK,
            selector='button[data-testid="submit"]',
            selector_candidates=('button[type="submit"]',),
        )
    )

    result = validator.validate(contract)
    assert result.allowed is True
    assert result.code == "OK"
