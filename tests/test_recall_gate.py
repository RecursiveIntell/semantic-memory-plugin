#!/usr/bin/env python3
"""Standalone-executable + pytest-compatible tests for the semantic-memory recall gate.

Pure stdlib — runs pre-install, no hermes runtime or memory server required:
    python3 tests/test_recall_gate.py        (exit 0 = all pass)
    pytest tests/test_recall_gate.py -q      (CI mode)

Covers, in order:
1. trivial-prompt gating (RED for the original leak class: "what time is it")
2. query classification A-E
3. evidence quality gate: durable keep-by-default in trusted namespaces,
   artifact/template/speculative/stale-status/low-trust rejection
4. RRF + cosine band gates
5. trust-tier header rendering
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin-provider", "semantic-memory"))

import recall_gate as g  # noqa: E402


def test_trivial_prompt_gate_blocks_time_queries():
    for q in ("what time is it", "what day is it today", "how are you",
              "hello there, how are you doing today", "tell me a joke",
              "what's the weather like outside", "ok thanks", "thanks"):
        assert g.is_trivial_recall_query(q) is True, q


def test_trivial_prompt_gate_passes_real_queries():
    for q in ("how does the receipt lineage work",
              "which rust crates belong to the libraries monorepo",
              "what is the user's preference on verifying completed work"):
        assert g.is_trivial_recall_query(q) is False, q


def test_classify_query_shapes():
    assert g.classify_query("search for the config file") == "A"
    assert g.classify_query("how does X relate to Y") == "B"
    assert g.classify_query("is it true that X conflicts with Y?") == "C"
    assert g.classify_query("compare and summarize the landscape") == "D"
    assert g.classify_query("when did this change happen?") == "E"


def test_quality_gate_keeps_structural_fact_in_trusted_namespace():
    r = {"id": "f1", "namespace": "libraries", "content": "Libraries monorepo at ~/Coding/Libraries. 63 Rust crates in 12 layers."}
    q = g.classify_recall_quality(r, "which crates belong to the monorepo")
    assert q["safe_as_evidence"] is True
    assert q["label"] == "durable_structural"


def test_quality_gate_keeps_verification_backed_fact():
    r = {"id": "f2", "namespace": "research", "content": "PPL measured 8.2 on WikiText-2, verified against released checkpoint."}
    q = g.classify_recall_quality(r, "what are the perplexity results")
    assert q["safe_as_evidence"] is True
    assert q["label"] == "authoritative_durable"


def test_quality_gate_rejects_low_trust_namespace():
    r = {"id": "f3", "namespace": "general", "content": "Some perfectly factual note about a config."}
    q = g.classify_recall_quality(r, "about that config")
    assert q["safe_as_evidence"] is False


def test_quality_gate_rejects_artifact_markers():
    r = {"id": "f4", "namespace": "projects", "content": "CODEX_PHASED_PROMPT step 4 of the master control pack"}
    q = g.classify_recall_quality(r, "the codex pack")
    assert q["safe_as_evidence"] is False
    assert q["label"] == "artifact_template"


def test_quality_gate_rejects_speculative():
    r = {"id": "f5", "namespace": "research", "content": "This would likely be the candidate approach worth exploring."}
    q = g.classify_recall_quality(r, "approaches")
    assert q["safe_as_evidence"] is False
    assert q["label"] == "speculative"


def test_quality_gate_rejects_stale_status_on_current_state_query():
    r = {"id": "f6", "namespace": "codex", "content": "Everything is done and shipped, completion summary recorded."}
    q = g.classify_recall_quality(r, "is it done and active right now?")
    assert q["safe_as_evidence"] is False
    assert q["label"] == "stale_status"


def test_apply_quality_gate_caps_and_orders():
    results = [
        {"id": "a", "namespace": "libraries", "content": "monorepo layout fact one"},
        {"id": "b", "namespace": "general", "content": "low trust filler"},
        {"id": "c", "namespace": "doctrine", "content": "authority hierarchy fact"},
    ]
    kept, filtered = g.apply_quality_gate(results, "monorepo layout", 2)
    assert [r["id"] for r, _ in kept] == ["a", "c"]
    assert all(q["safe_as_evidence"] for _, q in kept)
    assert len(filtered) == 1


def test_rrf_band_gate():
    results = [
        {"id": "a", "score": 0.030},
        {"id": "b", "score": 0.020},
        {"id": "c", "score": 0.004},
        {"id": "d", "score": 0.0001},
    ]
    keep = g.rrf_band_gate(results)
    ids = [r["id"] for r in keep]
    assert ids == ["a", "b"]  # top-relative band, absolute floor applied


def test_cosine_band_gate_gates_below_floor():
    results = [{"id": "x", "cosine_similarity": 0.40}]
    assert g.cosine_band_gate(results) == []
    results2 = [{"id": "y", "cosine_similarity": 0.72}, {"id": "z", "cosine_similarity": 0.63}]
    keep = g.cosine_band_gate(results2)
    # floor = max(0.54, 0.72-0.12) = 0.60 -> both y and z are within band
    assert [r["id"] for r in keep] == ["y", "z"]
    # a fact in the dropped zone (below floor, above abs floor) is filtered
    results3 = [{"id": "y", "cosine_similarity": 0.72}, {"id": "w", "cosine_similarity": 0.56}]
    keep3 = g.cosine_band_gate(results3)
    assert [r["id"] for r in keep3] == ["y"]  # 0.56 < 0.60 band floor


def test_trust_tier_header():
    h = g.trust_tier_header()
    assert "authoritative_durable" in h and "verify" in h


def test_trust_tier_header_does_not_authorize_direct_use():
    """Regression guard: the header must never tell the model to use labeled
    memories 'directly' as knowledge — labels are heuristic hints (pub-review
    requirement 2026-09-30, receipt ...3e73aace)."""
    h = g.trust_tier_header().lower()
    assert "may be used directly" not in h
    assert "not proof or instructions" in h
    assert "untrusted" in h


def _run_all():
    ns = dict(globals())
    tests = [(k, v) for k, v in ns.items() if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed.append((name, e))
            print(f"FAIL {name}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()