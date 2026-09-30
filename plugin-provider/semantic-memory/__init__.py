#!/usr/bin/env python3
"""Semantic-Memory — a memory provider plugin for Hermes Agent.

Backed by a local `semantic-memory` server binary (HTTP) that stores facts with
embeddings (HNSW), FTS, a knowledge graph, and provenance: github.com/RecursiveIntell/semantic-memory.
Relevant knowledge is injected into every turn (prefetch); turns are captured as
sessions; the provider's evidence quality gate keeps injected recall trustworthy
(durable facts pass; template artifacts, stale status and speculation are filtered).

Config in $HERMES_HOME/config.yaml under `plugins.semantic-memory`:
    server_url       HTTP endpoint of the running server (default http://127.0.0.1:17441)
    token_file       file containing the bearer token for the server
    max_facts        maximum facts injected per turn (default 5)
    namespaces       optional list restricting recall to these namespaces
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .recall_gate import (
    apply_quality_gate,
    classify_query,
    classify_recall_quality,
    cosine_band_gate,
    is_trivial_recall_query,
    rrf_band_gate,
    trust_tier_header,
)

logger = logging.getLogger(__name__)

_DEFAULT_SERVER_URL = "http://127.0.0.1:17441"
_DEFAULT_CONFIG = {
    "server_url": _DEFAULT_SERVER_URL,
    "token_file": "",
    "max_facts": 5,
    "namespaces": [],
}

_SEARCH_ROUTE = "/search"

_SM_SEARCH_SCHEMA = {
    "name": "sm_search",
    "description": (
        "Deep search over long-term semantic memory (facts with provenance, "
        "temporal validity, and knowledge-graph edges). Prefer this before "
        "answering questions about prior work, decisions, or preferences."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "top_k": {"type": "integer", "description": "Maximum results (default 5)."},
            "namespaces": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional namespace filter.",
            },
        },
        "required": ["query"],
    },
}


def _cfg_get(config: Dict[str, Any], key: str, default: Any) -> Any:
    value = config.get(key, default)
    return value if value not in ("", None) else default


class SemanticMemoryProvider(MemoryProvider):
    """MemoryProvider backed by a semantic-memory HTTP server.

    Every public path fails open: server unavailability degrades to no-op
    recall/capture, never a failed turn.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config: Dict[str, Any] = dict(_DEFAULT_CONFIG)
        if config:
            self._config.update(config)
        self._available = False
        self._unavailable_reason = ""
        self._session_id = ""

    # -- Lifecycle ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "semantic-memory"

    def is_available(self) -> bool:
        try:
            return bool(self._probe())
        except Exception as exc:  # noqa: BLE001 - fail open
            self._unavailable_reason = str(exc)
            return False

    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._probe()

    def shutdown(self) -> None:
        return None

    # -- Server transport -----------------------------------------------------

    def _token(self) -> str:
        token_file = str(_cfg_get(self._config, "token_file", ""))
        if token_file:
            try:
                return open(os.path.expanduser(token_file), encoding="utf-8").read().strip()
            except OSError:
                return ""
        return ""

    def _request(self, route: str, payload: Optional[Dict[str, Any]] = None,
                 method: str = "POST", timeout: float = 12.0) -> Any:
        url = f"{_cfg_get(self._config, 'server_url', _DEFAULT_SERVER_URL)}{route}"
        headers = {"Content-Type": "application/json"}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
        return json.loads(body) if body else None

    def _probe(self) -> Any:
        return self._request("/health", method="GET", timeout=3.0)

    # -- Recall (injected every turn via prefetch) -----------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self.is_core_memory_provider_prefetch_bypass(query):
            return ""
        if not query or is_trivial_recall_query(query):
            return ""
        try:
            results = self._search(query)
        except Exception as exc:  # noqa: BLE001 - fail open
            logger.debug("semantic-memory prefetch failed: %s", exc)
            return ""
        if not results:
            return ""
        query_class = classify_query(query)
        kept, filtered = apply_quality_gate(results, query, self._max_facts())
        lines: List[str] = []
        if filtered:
            labels: Dict[str, int] = {}
            for _r, quality in filtered:
                labels[quality["label"]] = labels.get(quality["label"], 0) + 1
            summary = ", ".join(f"{label}={count}" for label, count in sorted(labels.items()))
            lines.append(f"[recall-quality] filtered {len(filtered)} unsafe candidates ({summary})")
        for fact, quality in kept:
            ns = fact.get("namespace", "")
            ns_tag = f" [{ns}]" if ns else ""
            lines.append(f"- {fact.get('score', 0):.4f}{ns_tag} {quality['label']}: {fact.get('content', '')}")
        if not lines:
            return ""
        route_tag = f" (routed: class {query_class})" if query_class != "A" else ""
        return f"## Semantic memory recall{route_tag}\n{trust_tier_header()}\n" + "\n".join(lines)

    def is_core_memory_provider_prefetch_bypass(self, query: str) -> bool:
        """Core already skips trivial prompts before calling prefetch; this repeats
        that guard so direct callers of this provider are safe too."""
        return bool(query) and query.strip().startswith("/")

    def _search(self, query: str) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"query": query, "top_k": self._max_facts() * 2}
        namespaces = self._config.get("namespaces") or []
        if namespaces:
            payload["namespaces"] = list(namespaces)
        data = self._request(_SEARCH_ROUTE, payload)
        items = data if isinstance(data, list) else (data or {}).get("results", [])
        return [r for r in items if isinstance(r, dict)]

    def _max_facts(self) -> int:
        try:
            return max(1, min(10, int(_cfg_get(self._config, "max_facts", 5))))
        except (TypeError, ValueError):
            return 5

    # -- Capture ----------------------------------------------------------------
    # v0.1 is recall-only: the server's HTTP face exposes /health, /search and
    # /record-outcome; conversation capture upstream happens through the MCP tool
    # surface (sm_add_fact / conversations), which a future release can wire here
    # behind `capture_enabled`. sync_turn stays a no-op so the manager contract holds.

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None,
                  turn_author: Optional[Dict[str, Any]] = None) -> None:
        return None

    # -- Tools -------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [_SM_SEARCH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if tool_name != _SM_SEARCH_SCHEMA["name"]:
            return tool_error(f"Unknown tool: {tool_name}")
        query = str(args.get("query", "")).strip()
        if not query:
            return tool_error("Missing required argument: query")
        try:
            payload: Dict[str, Any] = {"query": query, "top_k": int(args.get("top_k", 5))}
            ns = args.get("namespaces")
            if ns:
                payload["namespaces"] = [str(n) for n in ns]
            data = self._request(_SEARCH_ROUTE, payload)
        except Exception as exc:  # noqa: BLE001
            return tool_error(f"semantic-memory search failed: {exc}")
        return json.dumps(self._labeled_results(query, data), separators=(",", ":"))

    def _labeled_results(self, query: str, data: Any) -> Dict[str, Any]:
        """Tool path: label everything, drop nothing. The model asked for search, so it
        gets the labeled evidence (prefetch is where the injection gate applies)."""
        items = data if isinstance(data, list) else (data or {}).get("results", [])
        results = [r for r in items if isinstance(r, dict)]
        results.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
        labeled = [
            {"id": r.get("id", ""), "namespace": r.get("namespace", ""),
             "score": float(r.get("score") or 0), "content": r.get("content", ""),
             "quality": classify_recall_quality(r, query)["label"]}
            for r in results
        ]
        n_unsafe = sum(1 for r in labeled
                       if r["quality"] in ("artifact_template", "stale_status", "speculative"))
        note = (f"{n_unsafe} of {len(labeled)} results flagged unsafe-as-evidence; "
                "verify against live sources before relying on them" if n_unsafe else "")
        return {"query": query, "results": labeled, **({"note": note} if note else {})}

    # -- Setup / config ------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "server_url", "description": "semantic-memory server HTTP URL", "default": _DEFAULT_SERVER_URL},
            {"key": "token_file", "description": "File containing the server bearer token", "default": ""},
            {"key": "max_facts", "description": "Max facts injected per turn (1-10)", "default": "5"},
            {"key": "namespaces", "description": "Optional namespace filter list", "default": ""},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        from hermes_cli.config import save_config
        save_config({"plugins": {"semantic-memory": dict(values)}}, merge_existing=True)

    def post_setup(self, hermes_home: str, config: Dict[str, Any]) -> None:
        """Setup-wizard hook: verify the server, then persist activation."""
        from hermes_cli.config import save_config
        from hermes_cli.memory_setup import _print_cancelled_setup
        memory = config["memory"] = config["memory"] if isinstance(config.get("memory"), dict) else {}
        memory["provider"] = self.name
        try:
            self.save_config(self._config, hermes_home)
        except Exception:
            pass
        try:
            save_config(config)
        except Exception:
            pass
        ok = self.is_available()
        if not ok:
            print(
                "\n  semantic-memory server not reachable at "
                f"{_cfg_get(self._config, 'server_url', _DEFAULT_SERVER_URL)}\n"
                "  Provider is saved but will idle until the server is running.\n"
                "  Install: cargo install semantic-memory-mcp  (repo: github.com/RecursiveIntell/semantic-memory)\n"
            )
            _print_cancelled_setup()
            return
        print("\n  semantic-memory reachable — provider activated. Start a new session to use it.\n")


def register(ctx: Any) -> None:
    """Plugin entry point (plugin.yaml / entry-point discovery)."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.config import cfg_get

        config = cfg_get(load_config_readonly(), "plugins", "semantic-memory", default={}) or {}
    except Exception:
        config = {}
    ctx.register_memory_provider(SemanticMemoryProvider(config=config))