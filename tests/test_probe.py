import json, os, sys, importlib.util
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "plugin-provider", "semantic-memory"))
import probe  # noqa: E402


def test_query_set_shape():
    assert len(probe.QUERIES) == 20
    cats = [probe.category_of(i) for i in range(20)]
    assert cats.count("should-hit") == 10
    assert cats.count("should-gate") == 5
    assert cats.count("adversarial") == 5


def test_probe_fixed_set_matches_operator_baseline():
    # The fixed set must not be tuned between comparisons (plan invariant).
    assert probe.QUERIES[0] == "how does the context governor compaction receipt lineage work"
    assert probe.QUERIES[10] == "what time is it"
    assert probe.QUERIES[-1] == "whats the current build order dag state"


def test_offline_run_fails_open_no_exception():
    # needs the upstream runtime importable (provider imports agent.*) when available;
    # skip cleanly in bare environments
    try:
        import agent.memory_provider  # noqa: F401
    except ModuleNotFoundError:
        try:
            import pytest
            pytest.skip("upstream runtime not importable")
        except ImportError:
            print("SKIP offline test (no runtime)"); return
    results = probe.run(server_url="http://127.0.0.1:9", token_file=os.devnull,
                        ledger_path=os.devnull)
    assert len(results) == 20
    assert all(not r.get("fact_lines") for r in results)


if __name__ == "__main__":
    ns = dict(globals())
    tests = [(k, v) for k, v in ns.items() if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:
            failed.append(name)
            print("FAIL", name, e)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    sys.exit(1 if failed else 0)