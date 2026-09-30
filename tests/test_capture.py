import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin-provider", "semantic-memory"))
import capture as cap


def test_extraction_picks_durable_statements():
    cands = cap.extract_candidates("I prefer cargo test over pytest for this repo", "Sure, noted.")
    assert len(cands) == 1
    assert cands[0]["namespace"] == "conversation"
    assert "cargo test" in cands[0]["content"]


def test_extraction_picks_user_reference():
    cands = cap.extract_candidates("the user decided to standardize on rust 1.90", "ok")
    assert len(cands) == 1


def test_extraction_ignores_smalltalk():
    assert cap.extract_candidates("ok thanks", "sure!") == []


def test_extraction_ignores_questions():
    assert cap.extract_candidates("do you prefer tabs or spaces?", "spaces") == []


def test_dedupe_repeated_statements():
    a = cap.extract_candidates("I prefer vim bindings", "noted")
    b = cap.extract_candidates("I prefer vim bindings", "noted again")
    assert a and b
    assert a[0]["content_hash"] == b[0]["content_hash"]


def test_queue_flush_calls_add_fact(monkeypatch=None):
    calls = []
    class FakeMcp:
        def tool_call(self, name, args):
            calls.append((name, args))
            return {"content": [{"type": "text",
                    "text": json.dumps({"ok": True, "fact_id": "f1"})}]}
    q = cap.CaptureQueue(mcp_factory=lambda: FakeMcp(), flush_turns=2)
    q.sync_turn("I prefer the blue theme", "noted", "s1")
    assert calls == []  # queue holds until flush_turns
    q.sync_turn("The build process uses make, not cargo", "ok", "s1")
    flushed = [c for c in calls if c[0] == "sm_add_fact"]
    assert flushed, "flush after 2 turns expected"
    nss = [a.get("namespace") for _n, a in flushed]
    assert all(ns == "conversation" for ns in nss)


def test_fail_open_bad_mcp():
    def bad_factory():
        raise RuntimeError("down")
    q = cap.CaptureQueue(mcp_factory=bad_factory, flush_turns=1)
    q.sync_turn("I prefer tea", "ok", "s")
    # no exception; queued statement retained (re-held)


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