import asyncio

from app.core.v2.contracts import ActionExecutionResult
from app.core.v2.intent_cache import IntentWorkflowCache
from app.core.v2.intent_executor import IntentExecutor
from app.core.v2.perception import ActionCandidate
from app.core.v2.security_layer import SecurityPolicy


class _StubElement:
    selector_hints = ("#sign-in", "#name")


class _StubPerception:
    def __init__(self, candidates):
        self._candidates = candidates

    async def observe(self, intent, page, state):
        return self._candidates

    async def extract(self, instruction, page):
        return {"instruction": instruction}


class _StubExtractorState:
    url = "https://example.test"
    state_hashes = {"url": "u1", "elements": "e1"}
    interactive_elements = (_StubElement(),)


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
        if contract.intent == "intent bootstrap":
            return ActionExecutionResult(action_id="bootstrap", success=True)
        if len(self.calls) == 2:
            return ActionExecutionResult(action_id="first", success=False, failure_code="ACTION_EXECUTION_FAILED")
        return ActionExecutionResult(action_id="second", success=True)


def test_intent_executor_caches_final_contract(monkeypatch, tmp_path):
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
    assert len(cached["contracts"]) == 1
    assert cached["contracts"][0]["action_spec"]["action_type"] == "click"
    assert cached["state_hashes"]["elements"] == "e1"


def test_intent_executor_invalidates_stale_cache(monkeypatch, tmp_path):
    from app.core.v2 import intent_executor as module

    async def _fake_extract(self, prev_state_id, downloads):
        return _StubExtractorState()

    monkeypatch.setattr(module.StructuredStateExtractor, "extract", _fake_extract)

    class CountingPerception(_StubPerception):
        def __init__(self, candidates):
            super().__init__(candidates)
            self.calls = 0

        async def observe(self, intent, page, state):
            self.calls += 1
            return await super().observe(intent, page, state)

    candidates = [ActionCandidate("Sign in button", "click", "#sign-in", 0.8, {})]
    perception = CountingPerception(candidates)
    cache = IntentWorkflowCache(str(tmp_path / "intent.db"))
    key = module.WorkflowCacheKey("sign in", "https://example.test", "default")
    cache.put(
        key,
        {
            "contracts": [
                {
                    "workflow_id": "w1",
                    "run_id": "old",
                    "step_index": 1,
                    "intent": "sign in",
                    "preconditions": [],
                    "action_spec": {"action_type": "click", "selector": "#sign-in"},
                    "expected_postconditions": [],
                    "verification_rules": [],
                    "wait_conditions": [],
                    "timeout": {},
                    "retry": {},
                    "escalation": {},
                    "metadata": {},
                }
            ],
            "state_hashes": {"url": "stale", "elements": "stale"},
        },
    )

    engine = _StubEngine()
    executor = IntentExecutor(engine=engine, perception=perception, cache=cache)
    result = asyncio.run(
        executor.execute_intent(
            tenant_id="t1",
            workflow_id="w1",
            policy=SecurityPolicy(allow_domains=("example.test",)),
            run_id="r1",
            step_index=1,
            intent="sign in",
        )
    )

    assert perception.calls == 1
    assert result["mode"] == "perception"
