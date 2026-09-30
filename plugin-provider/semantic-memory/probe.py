#!/usr/bin/env python3
"""Fixed-set probe harness: measures a provider install's recall-injection yield.

Ported from the operator deployment (2026-09-30 measurement round; baseline and
final snapshots on file). The 20-query set is FIXED — do not tune queries between
comparisons; yield deltas must come from store curation or gate changes, not probe
drift. Composition: 10 should-hit (durable namespaces), 5 should-gate (trivial),
5 adversarial (artifact-prone domains).

Usage:
    python3 -m probe --json out.json          (from this directory)
    python3 probe.py --json out.json
Reads provider config from $HERMES_HOME/config.yaml (memory.semantic-memory,
then plugins.semantic-memory). Runs the real provider path (gate + ledger), so
the numbers describe the actually installed behavior.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from recall_gate import is_trivial_recall_query, classify_query
from injection_ledger import default_path as ledger_default_path

# -- Fixed query set (do not tune between comparisons) -------------------------

QUERIES: List[str] = [
    # should-hit: durable namespaces (research, doctrine, infrastructure, libraries, preferences)
    "how does the context governor compaction receipt lineage work",
    "what did we decide about turbo-quant release claims",
    "how is the agent graph daemon supposed to be operated",
    "what are the provenance rules for semantic memory facts",
    "which rust crates belong to the libraries monorepo",
    "what is the users preference on verifying completed work",
    "how do hermes shell hooks inject context into the prompt",
    "what infrastructure services run on this laptop",
    "summarize what we know about poly-kv perplexity validation",
    "how does recall admission work for semantic memory",
    # should-gate: trivial
    "what time is it",
    "how are you",
    "tell me a joke",
    "ok thanks",
    "hello there, how are you doing today",
    # adversarial: domains where artifacts polluted the operator KB
    "determine how ready we are to start injecting memory into the runtime instead of relying on tools",
    "what is missing from the codex finish pack",
    "review the phase completion status of the super pass",
    "give me the executive intake for the next codex run",
    "whats the current build order dag state",
]

_N_SHOULD_HIT = 10
_N_SHOULD_GATE = 5


def category_of(index: int) -> str:
    if index < _N_SHOULD_HIT:
        return "should-hit"
    if index < _N_SHOULD_HIT + _N_SHOULD_GATE:
        return "should-gate"
    return "adversarial"


def _resolve_config(explicit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if explicit:
        return dict(explicit)
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.config import cfg_get

        cfg = load_config_readonly() or {}
        merged = dict(cfg_get(cfg, "plugins", "semantic-memory", default={}) or {})
        merged.update(cfg_get(cfg, "memory", "semantic-memory", default={}) or {})
        return merged
    except Exception:
        return {}


def run(server_url: Optional[str] = None, token_file: Optional[str] = None,
        ledger_path: Optional[str] = None,
        explicit_config: Optional[Dict[str, Any]] = None,
        quiet: bool = False) -> List[Dict[str, Any]]:
    """Run all 20 queries through a real provider instance. Offline server ->
    every query gates/empty (fail-open contract), never raises."""
    cfg = _resolve_config(explicit_config)
    if server_url:
        cfg["server_url"] = server_url
    if token_file is not None:
        cfg["token_file"] = token_file

    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("probe_provider_pkg", os.path.join(here, "__init__.py"),
                                                  submodule_search_locations=[here])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["probe_provider_pkg"] = mod
    spec.loader.exec_module(mod)
    provider = mod.SemanticMemoryProvider(config=cfg)
    if ledger_path is not None:
        provider._ledger_path = ledger_path
        provider._ledger_enabled = ledger_path != os.devnull
    provider.initialize("probe")

    store_note = ""
    try:
        if provider.is_available():
            store_note = "server reachable"
        else:
            store_note = f"server UNREACHABLE ({provider.unavailable_reason()}) - results all empty"
    except Exception:
        store_note = "server unreachable - results all empty"

    results: List[Dict[str, Any]] = []
    for i, q in enumerate(QUERIES):
        try:
            text = provider.prefetch(q, session_id="probe") or ""
        except Exception:
            text = ""
        fact_lines = [l for l in text.split("\n") if l.strip().startswith("- ")]
        r = {
            "query": q,
            "category": category_of(i),
            "injected": bool(text),
            "fact_lines": len(fact_lines),
        }
        results.append(r)
        if not quiet:
            tag = f"{r['fact_lines']} facts" if r["fact_lines"] else ("inj-empty" if r["injected"] else "GATED")
            print(f"[{r['category']:>10}] [{tag:>9}] {q[:58]}")

    hits = sum(1 for r in results if r["category"] == "should-hit" and r["fact_lines"])
    gated = sum(1 for r in results if r["category"] == "should-gate" and not r["injected"])
    if not quiet:
        print(f"\nstore: {store_note}")
        print(f"should-hit yield:  {hits}/{_N_SHOULD_HIT} ({hits / _N_SHOULD_HIT:.0%})")
        print(f"trivial gated:     {gated}/{_N_SHOULD_GATE}")
        if hits == 0 and "UNREACHABLE" in store_note:
            print("(0/10 with an unreachable or empty store means nothing is measured - "
                  "fill the store, then re-run)")
    return results


def main() -> None:
    try:
        import agent.memory_provider  # noqa: F401  (provider needs the runtime)
    except ModuleNotFoundError:
        print("The provider needs the Hermes runtime importable. Run the probe with the\n"
              "runtime python, e.g.:\n"
              "  <hermes-runtime>/.venv/bin/python -m probe --json /tmp/probe.json\n"
              "(or add the runtime source root to PYTHONPATH)")
        sys.exit(2)
    ap = argparse.ArgumentParser(description="semantic-memory recall-injection probe")
    ap.add_argument("--json", default="", help="write results JSON here")
    ap.add_argument("--server-url", default=None)
    ap.add_argument("--token-file", default=None)
    a = ap.parse_args()
    results = run(server_url=a.server_url, token_file=a.token_file)
    if a.json:
        hits = sum(1 for r in results if r["category"] == "should-hit" and r["fact_lines"])
        with open(a.json, "w") as f:
            json.dump({"hit_rate": hits / _N_SHOULD_HIT,
                       "results": results}, f, indent=2)
        print(f"saved: {a.json}")


if __name__ == "__main__":
    main()