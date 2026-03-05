from app.core.v2.intent_cache import IntentWorkflowCache, WorkflowCacheKey


def test_intent_cache_roundtrip(tmp_path):
    cache = IntentWorkflowCache(str(tmp_path / "intent.db"))
    key = WorkflowCacheKey(instruction="click checkout", start_url="https://shop.test", environment="dev")

    assert cache.get(key) is None

    cache.put(
        key,
        {
            "contracts": [
                {
                    "workflow_id": "wf",
                    "run_id": "run",
                    "step_index": 1,
                    "intent": "click checkout",
                    "action_spec": {"action_type": "click", "selector": "#checkout"},
                }
            ],
            "state_hashes": {"url": "abc", "elements": "xyz"},
        },
    )
    hit = cache.get(key)

    assert hit is not None
    assert hit["contracts"][0]["action_spec"]["selector"] == "#checkout"

    cache.invalidate(key)
    assert cache.get(key) is None
