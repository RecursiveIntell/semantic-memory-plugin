#!/usr/bin/env python3
"""Standalone/pytest tests for the injection ledger (stdlib only)."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin-provider", "semantic-memory"))
import injection_ledger as led


def _fresh():
    return os.path.join(tempfile.mkdtemp(), "ledger.jsonl")


def test_record_keeps_and_filters():
    path = _fresh()
    led.record(path, query="which crates are in the monorepo", query_class="A",
               kept=[({"id": "a"}, {"label": "durable_structural"})],
               filtered=[({"id": "b"}, {"label": "artifact_template"})])
    rec = json.loads(open(path).read().strip())
    assert rec["kept_n"] == 1 and rec["filtered_n"] == 1
    assert rec["kept_labels"] == {"durable_structural": 1}
    assert rec["injected"] is True and rec["query_class"] == "A"
    assert len(rec["query"]) <= 200 and rec["ts"]


def test_record_empty_is_injected_false():
    path = _fresh()
    led.record(path, query="hi", query_class="A",
               kept=[], filtered=[({"id": "x"}, {"label": "background"})])
    rec = json.loads(open(path).read().strip())
    assert rec["injected"] is False and rec["filtered_labels"] == {"background": 1}


def test_fail_open_on_bad_path():
    led.record("/nonexistent-dir-xyz/ledger.jsonl", "q", "A", [], [])  # must not raise
    led.record(None, "q", "A", [], [])  # must not raise


def test_default_path_fallback():
    p = led.default_path()
    assert p and ("semantic-memory" in p) and p.endswith("injection-ledger.jsonl")


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