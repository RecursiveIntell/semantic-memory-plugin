import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin-provider", "semantic-memory"))

def test_routing_disabled_uses_flat_only():
    # fake module with no runtime available: direct unit via monkeypatch-free construction
    import types, importlib.util
    DIR = os.path.join(os.path.dirname(__file__), "..", "plugin-provider", "semantic-memory")
    spec = importlib.util.spec_from_file_location('rt1', DIR + '/__init__.py', submodule_search_locations=[DIR])
    m = importlib.util.module_from_spec(spec); sys.modules['rt1'] = m
    try:
        spec.loader.exec_module(m)
    except ModuleNotFoundError:
        try:
            import pytest; pytest.skip("runtime not importable")
        except ImportError:
            return
    p = m.SemanticMemoryProvider(config={"routed_search": "false"})
    assert p._mcp_client() is None


def test_overlap_rerank_orders_by_relevance():
    import importlib.util, sys
    DIR = os.path.join(os.path.dirname(__file__), "..", "plugin-provider", "semantic-memory")
    spec = importlib.util.spec_from_file_location('rt2', DIR + '/__init__.py', submodule_search_locations=[DIR])
    m = importlib.util.module_from_spec(spec); sys.modules['rt2'] = m
    try:
        spec.loader.exec_module(m)
    except ModuleNotFoundError:
        try:
            import pytest; pytest.skip("runtime not importable")
        except ImportError:
            return
    results = [
        {"id": "unrelated", "content": "completely different topic about gardens", "score": 0.9},
        {"id": "on-topic", "content": "poly-kv perplexity validation protocol", "score": 0.5},
    ]
    reranked = m.SemanticMemoryProvider(config={})._overlap_rerank("poly-kv perplexity validation", results)
    assert reranked[0]["id"] == "on-topic"
