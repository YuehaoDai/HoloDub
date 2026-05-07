from app.services.model_registry import ModelRegistry


def test_registry_loads_only_once():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return f"value-{calls['n']}"

    r = ModelRegistry()
    assert r.get_or_load("a", loader) == "value-1"
    assert r.get_or_load("a", loader) == "value-1"
    assert calls["n"] == 1


def test_status_lists_keys_in_access_order():
    r = ModelRegistry()
    r.get_or_load("a", lambda: 1)
    r.get_or_load("b", lambda: 2)
    r.get_or_load("a", lambda: 99)  # access bumps to end
    assert r.status() == ["b", "a"]


def test_lru_eviction_when_max_models_set():
    r = ModelRegistry(max_models=2)
    r.get_or_load("a", lambda: 1)
    r.get_or_load("b", lambda: 2)
    r.get_or_load("c", lambda: 3)
    # 'a' was the least-recently-used and should have been evicted.
    assert r.status() == ["b", "c"]


def test_lru_keeps_recently_used():
    r = ModelRegistry(max_models=2)
    r.get_or_load("a", lambda: 1)
    r.get_or_load("b", lambda: 2)
    # Touch 'a' so 'b' becomes the LRU.
    r.get_or_load("a", lambda: 99)
    r.get_or_load("c", lambda: 3)
    assert r.status() == ["a", "c"]


def test_unload_removes_only_target():
    r = ModelRegistry()
    r.get_or_load("a", lambda: 1)
    r.get_or_load("b", lambda: 2)
    assert r.unload("a") is True
    assert r.unload("a") is False
    assert r.status() == ["b"]


def test_clear_drops_everything():
    r = ModelRegistry()
    for k in ["a", "b", "c"]:
        r.get_or_load(k, lambda: 0)
    assert r.clear() == 3
    assert r.status() == []
