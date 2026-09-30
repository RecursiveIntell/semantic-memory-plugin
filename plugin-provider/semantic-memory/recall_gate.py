#!/usr/bin/env python3
"""Recall quality gate for the semantic-memory provider.

Distilled from an operator deployment that measured recall precision across a
fixed 20-query probe (trivial / durable / adversarial domains). Design invariants:

- Recall is DISCOVERY, not proof: injected facts carry trust tiers, and
  current-state phrasing ("is X shipped?") adds a live-verification note.
- Negative gates dominate: template artifacts, prompt-pack scaffolding,
  speculative language, stale status claims, and low-trust namespaces
  never inject, regardless of search score.
- Inside a trusted namespace, clean content keeps by default
  (``durable_structural``): structural/architecture facts need no
  verification keyword to be durable.
- The score gates are relative-band with an absolute floor, per score family:
  fused RRF scores (warm server) and cosine similarity (cold path).

Pure stdlib: this module must stay importable without the Hermes runtime so
standalone tests and embedders can validate gate behavior cheaply.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple

# -- Trivial-prompt gating (skip recall; saves a round-trip and tokens) --------

_STOPWORDS = {
    "about", "after", "again", "agent", "agentic", "and", "best", "code",
    "coding", "does", "doing", "everything", "examine", "for", "from",
    "function", "have", "how", "improve", "into", "look", "looks", "make",
    "optimize", "perform", "performance", "possible", "research", "seem",
    "seems", "than", "that", "the", "this", "through", "using", "well",
    "what", "when", "where", "with", "your",
}

_TRIVIAL_PATTERNS = (
    r"^(what|whats|what's)\s+(time|day|date)\b",
    r"\bhow\s+(are|r)\s+(you|u)\b",
    r"\bweather\b",
    r"^(tell|say)\s+(me\s+)?(a\s+)?(joke|story|funny)\b",
    r"^(hi|hey|hello|yo|sup|hiya)\b[\s,!.]+$",
)

_SKIP_PHRASES = {
    "ok", "yes", "no", "thanks", "done", "sure", "yeah", "right", "correct",
    "agreed", "got it", "sounds good", "that works", "makes sense", "understood",
    "gotcha", "do it", "fix it", "go for it", "continue",
}


def _terms(text: str) -> set:
    return {m.group() for m in re.finditer(r"[a-zA-Z][a-zA-Z0-9_-]+", (text or "").lower())
            if m.group() not in _STOPWORDS and len(m.group()) > 1}


def is_trivial_recall_query(query: str) -> bool:
    """True when the prompt carries no durable-knowledge content."""
    prompt = (query or "").strip()
    if len(prompt) < 12 or prompt.startswith("/"):
        return True
    low = prompt.lower()
    if low in _SKIP_PHRASES or any(low.startswith(p) and len(low) <= len(p) + 10
                                   for p in ("can you", "could you", "would you", "will you")):
        return True
    if any(re.search(p, low) for p in _TRIVIAL_PATTERNS):
        return True
    return len(_terms(prompt)) < 2


# -- Query classification (routes retrieval strategy and the header tag) ------

def classify_query(query: str) -> str:
    """A=simple flat; B=multi-hop; C=contradiction; D=synthesis; E=temporal."""
    q = (query or "").lower()
    if any(w in q for w in ("contradiction", "conflict", "disagree", "but", "vs", "versus", "is it true", "wrong")):
        return "C"
    if any(w in q for w in ("summarize", "overview", "all about", "themes", "landscape", "everything", "compare", "comparison")):
        return "D"
    if any(w in q for w in ("when", "before", "after", "changed", "current", "latest", "updated", "how old", "timeline")):
        return "E"
    if len(_terms(query)) >= 2 and any(w in q for w in ("connects", "between", "depends on", "relates to", "relationship", "how did", "how does", "work with", "lead to", "link", "integrate")):
        return "B"
    return "A"


# -- Evidence quality gate ------------------------------------------------------

AUTHORITATIVE_DURABLE_NAMESPACES = {
    "research", "semantic-memory", "libraries", "libraries-crates",
    "doctrine", "infrastructure", "preferences", "recursiveintell",
    "behavioral", "agent-setup", "personal",
}

LOW_TRUST_NAMESPACES = {"autonomous", "tool-receipts", "general", "test",
                        "quarantine-artifacts", "archive-stale"}

ARTIFACT_MARKERS: Tuple[str, ...] = (
    "PHASE_", "P00_", "P01_", "P02_", "P03_", "P04_", "P05_", "P06_", "P07_",
    "P08_", "P09_", "P10_", "P11_", "P12_", "P13_", "P14_", "P15_", "P16_",
    "P17_", "P18_", "P19_", "P20_", "P21_",
    "AFTER_PHASE_", "CODEX_PHASED", "CODEX_CONTROL_PACK", "CODEX_EXEC_PHASE",
    "CODEX_WORKFLOW", "CODEX_OUTPUT_INTEGRATION", "START_HERE_PHASED",
    "OPERATOR_PASTE", "COPY_PASTE_SEQUENCE", "MASTER_PROMPT",
    "BUILD_ORDER_DAG", "RELEASE_BAR", "CONFORMANCE_PLAN", "EXECUTIVE_INTAKE",
    "PROMOTION_CANDIDATES", "IMPLEMENTATION_TIMELINE", "FINAL_AUDITOR",
    "NEXT_CODEX_RUN", "OPERATOR_DECISION", "convergence_spec", "super_pass",
    "00_OPERATOR", "00_README", "00_START", "01_EXECUTIVE",
)

_TEMPLATE_MARKERS: Tuple[str, ...] = ("template", "prompt template", "copy/paste", "copy-paste")

_SPECULATIVE_RE = re.compile(
    r"\b(likely|potentially|may indicate|could imply|seems to|probably|"
    r"candidate|proposal|proposed|should build|would build)\b")

_VERIFICATION_RE = re.compile(
    r"\b(source:|verified|passed|cargo test|cargo check|arxiv:|github api|"
    r"crates\.io|receipt|evidence:)\b")

_LIVE_STATUS_RE = re.compile(
    r"\b(everything is done|done and shipped|shipped|completed|completion summary|"
    r"status update|phase complete)\b")

_CURRENT_STATE_QUERY_RE = re.compile(
    r"\b(changed|current|latest|now|done|shipped|implemented|active|running|"
    r"status|is it done|did it ship|did we ship|how did|what changed|right now)\b")

LIVE_VERIFICATION_NOTE = (
    "[live-verification] Current-state/status query: semantic memory is discovery, "
    "not proof. Verify live config/process/repo/API state before final claims."
)


def classify_recall_quality(result: Dict[str, Any], query: str) -> Dict[str, Any]:
    """Label one search result. Returns {"label", "safe_as_evidence", "reasons"}."""
    content = (result.get("content") or "")
    low = content.lower()
    namespace = (result.get("namespace") or "").lower()
    reasons: List[str] = []

    if result.get("_graph_discovery"):
        reasons.append("graph-discovery-not-proof")
    for marker in ARTIFACT_MARKERS:
        if marker.lower() in low:
            reasons.append("artifact-marker")
            break
    for marker in _TEMPLATE_MARKERS:
        if marker.lower() in low:
            reasons.append("template-or-prompt-artifact")
            break
    if _LIVE_STATUS_RE.search(low) and _CURRENT_STATE_QUERY_RE.search((query or "").lower()):
        reasons.append("stale-status-claim")
    if _SPECULATIVE_RE.search(low) and not _VERIFICATION_RE.search(low):
        reasons.append("speculative-language")
    if _VERIFICATION_RE.search(low):
        reasons.append("verification-signal")
    if namespace in LOW_TRUST_NAMESPACES:
        reasons.append(f"low-trust-namespace:{namespace}")

    stale = "stale-status-claim" in reasons
    speculative = "speculative-language" in reasons
    unsafe = any(r.startswith(("artifact-marker", "template-or-prompt-artifact",
                               "low-trust-namespace")) for r in reasons)

    if "graph-discovery-not-proof" in reasons:
        label, safe = "graph_discovery", False
    elif stale:
        label, safe = "stale_status", False
    elif unsafe:
        label, safe = "artifact_template", False
    elif speculative:
        label, safe = "speculative", False
    elif "verification-signal" in reasons and namespace in AUTHORITATIVE_DURABLE_NAMESPACES:
        label, safe = "authoritative_durable", True
    elif namespace in AUTHORITATIVE_DURABLE_NAMESPACES:
        label, safe = "durable_structural", True
    else:
        label, safe = "background", False
    return {"label": label, "safe_as_evidence": safe, "reasons": reasons or ["no-quality-flags"]}


def apply_quality_gate(results: Iterable[Dict[str, Any]], query: str,
                       max_hits: int = 5) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]],
                                                   List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
    """Split scored results into (kept, filtered) (result, quality) pairs."""
    kept: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    filtered: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for result in results:
        quality = classify_recall_quality(result, query)
        if (result.get("namespace") or "").lower() == "tool-receipts":
            filtered.append((result, quality))
        elif quality["safe_as_evidence"] and len(kept) < max_hits:
            kept.append((result, quality))
        else:
            filtered.append((result, quality))
    return kept, filtered


# -- Score band gates ------------------------------------------------------------

def rrf_band_gate(results: List[Dict[str, Any]], *, relative: float = 0.4,
                  floor: float = 0.005, max_hits: int = 5) -> List[Dict[str, Any]]:
    """Warm-path gate over fused RRF scores (typical range ~0.01-0.03)."""
    if not results:
        return []
    ordered = sorted(results, key=lambda r: float(r.get("score") or 0), reverse=True)
    top = float(ordered[0].get("score") or 0)
    if top <= 0:
        return []
    threshold = max(top * relative, floor)
    return [r for r in ordered if float(r.get("score") or 0) >= threshold][:max_hits]


def cosine_band_gate(results: List[Dict[str, Any]], *, min_top: float = 0.58,
                     band: float = 0.12, abs_floor: float = 0.54,
                     max_hits: int = 5) -> List[Dict[str, Any]]:
    """Cold-path gate over cosine similarity (nomic-class embedders sit ~0.5 at baseline)."""
    if not results:
        return []
    ordered = sorted(results, key=lambda r: float(r.get("cosine_similarity") or 0), reverse=True)
    top = float(ordered[0].get("cosine_similarity") or 0)
    if top < min_top:
        return []
    threshold = max(abs_floor, top - band)
    return [r for r in ordered if float(r.get("cosine_similarity") or 0) >= threshold][:max_hits]


# -- Trust-tier header (rendered above injected facts) ----------------------------

def trust_tier_header() -> str:
    return (
        "[RECALLED-MEMORY — trust tiers: items labeled authoritative_durable or "
        "durable_structural may be used directly as durable knowledge (cite the namespace); "
        "items labeled background/speculative are hints only — verify before use; "
        "current-state claims still require live verification against repo, config, "
        "process, or API evidence.]"
    )