#!/usr/bin/env python3
"""Injection outcome ledger: one JSONL line per eligible recall turn.

Operator-proven design (deployment receipt 2026-09-30): records kept/filtered
label counts so recall yield is measurable and regressions observable.
Fail-open: never blocks the turn.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple


def default_path() -> str:
    try:
        from hermes_constants import get_hermes_home
        return os.path.join(str(get_hermes_home()), "semantic-memory", "injection-ledger.jsonl")
    except Exception:
        return "~/.hermes/semantic-memory/injection-ledger.jsonl"


def record(path: Optional[str], query: str, query_class: str,
           kept: List[Tuple[Dict[str, Any], Dict[str, Any]]],
           filtered: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
    """Append one JSONL record. Silently no-ops on any error (fail-open)."""
    try:
        if not path:
            return
        path = os.path.expanduser(str(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)

        def _labels(pairs):
            d: Dict[str, int] = {}
            for _r, q in (pairs or []):
                lbl = (q or {}).get("label", "unknown")
                d[lbl] = d.get(lbl, 0) + 1
            return d

        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "query": (query or "")[:200],
            "query_class": query_class,
            "kept_n": len(kept or []),
            "filtered_n": len(filtered or []),
            "kept_labels": _labels(kept),
            "filtered_labels": _labels(filtered),
            "injected": bool(kept),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass