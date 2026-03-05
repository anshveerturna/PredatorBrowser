import asyncio

from app.core.v2.contracts import ActionExecutionResult
from app.core.v2.intent_cache import IntentWorkflowCache
from app.core.v2.intent_executor import IntentExecutor
from app.core.v2.perception import ActionCandidate
from app.core.v2.security_layer import SecurityPolicy


class _StubPerception:
    def __init__(self, candidates):
        self._candidates = candidates

    async def observe(self, intent, page, state):
        return self._candidates

    async def extract(self, instruction, page):
        return {"instruction": instruction}


class _StubExtractorState:
    url = "https://example.test"


class _StubNetwork:
    pass


class _StubPage:
    url = "https://example.test"


class _StubSession:
    page = _StubPage()
    network_observer = _StubNetwork()


class _StubSessions:
    def get_session(self, workflow_id):
        return _StubSession()


class _StubEngine:
    def __init__(self):
        self._sessions = _StubSessions()
        self.calls = []

    async def execute_contract(self, tenant_id, workflow_id, policy, contract):
        self.calls.append(contract)
        # bootstrap succeeds, first action fails, second action succeeds
        if contract.intent == "intent bootstrap":
            return ActionExecutionResult(action_id="bootstrap", success=True)
        if len(self.calls) == 2:
            return ActionExecutionResult(action_id="first", success=False, failure_code="ACTION_EXECUTION_FAILED")
        return ActionExecutionResult(action_id="second", success=True)


def test_intent_executor_caches_fallback_action(monkeypatch, tmp_path):
    from app.core.v2 import intent_executor as module

    async def _fake_extract(self, prev_state_id, downloads):
        return _StubExtractorState()

    monkeypatch.setattr(module.StructuredStateExtractor, "extract", _fake_extract)

    engine = _StubEngine()
    candidates = [
        ActionCandidate("Name input", "type", "#name", 0.9, {}),
        ActionCandidate("Sign in button", "click", "#sign-in", 0.8, {}),
    ]
    cache = IntentWorkflowCache(str(tmp_path / "intent.db"))
    executor = IntentExecutor(engine=engine, perception=_StubPerception(candidates), cache=cache)

    result = asyncio.run(
        executor.execute_intent(
            tenant_id="t1",
            workflow_id="w1",
            policy=SecurityPolicy(allow_domains=("example.test",)),
            run_id="r1",
            step_index=1,
            intent="sign in",
            type_text="alice",
        )
    )

    assert result["result"]["success"] is True
    key = module.WorkflowCacheKey("sign in", "https://example.test", "default")
    cached = cache.get(key)
    assert cached is not None
    assert cached["action_spec"]["action_type"] == "click"
