from app.core.v2.intent_cache import IntentWorkflowCache, WorkflowCacheKey


def test_intent_cache_roundtrip(tmp_path):
    cache = IntentWorkflowCache(str(tmp_path / "intent.db"))
    key = WorkflowCacheKey(instruction="click checkout", start_url="https://shop.test", environment="dev")

    assert cache.get(key) is None

    cache.put(key, {"action_spec": {"action_type": "click", "selector": "#checkout"}})
    hit = cache.get(key)

    assert hit is not None
    assert hit["action_spec"]["selector"] == "#checkout"

    cache.invalidate(key)
    assert cache.get(key) is None
