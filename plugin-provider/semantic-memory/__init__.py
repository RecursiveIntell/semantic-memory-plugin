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
    "ledger_enabled": "true",
    "ledger_path": "",
    "routed_search": "true",
    "mcp_url": "",
    "mcp_token_file": "",
    "capture_enabled": "false",
    "capture_flush_turns": 4,
}

_ROUTED_CLASSES = frozenset({"B", "C", "D", "E"})

_SEARCH_ROUTE = "/search"
_DEFAULT_MCP_PORT = 17440

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


def mcp_client_unwrap(data: Any) -> list:
    from . import mcp_client
    return mcp_client.unwrap_results(data)


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
        from . import injection_ledger as led
        self._ledger_path = str(_cfg_get(self._config, "ledger_path", "") or led.default_path())
        self._ledger_enabled = str(_cfg_get(self._config, "ledger_enabled", "true")).lower() in ("1", "true", "yes", "on")
        self._routed_enabled = str(_cfg_get(self._config, "routed_search", "true")).lower() in ("1", "true", "yes", "on")
        self._mcp: Any = None
        self._capture_enabled = str(_cfg_get(self._config, "capture_enabled", "false")).lower() in ("1", "true", "yes", "on")
        if self._capture_enabled:
            from . import capture
            flush_turns = int(_cfg_get(self._config, "capture_flush_turns", 4) or 4)
            self._capture = capture.CaptureQueue(self._mcp_factory_for_capture, flush_turns=flush_turns)
        else:
            self._capture = None

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
        try:
            self._probe()
        except Exception as exc:  # noqa: BLE001 - fail open (I1)
            self._available = False
            self._unavailable_reason = str(exc)

    def shutdown(self) -> None:
        if self._capture is not None:
            try:
                self._capture.flush()
            except Exception:  # noqa: BLE001
                pass

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
        if self._ledger_enabled:
            from . import injection_ledger
            injection_ledger.record(self._ledger_path, query, query_class, kept, filtered)
        self._record_outcome(query, query_class, kept)
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

    def _mcp_client(self) -> Any:
        """Lazily built MCP client (None when disabled/unreachable — fail open)."""
        if self._mcp is not None:
            return self._mcp
        if not self._routed_enabled:
            return None
        try:
            from . import mcp_client
            explicit = str(_cfg_get(self._config, "mcp_url", "") or "")
            if explicit:
                mcp_url = explicit if explicit.endswith("/mcp") else explicit + "/mcp"
            else:
                # derive from server_url host so custom hosts work
                from urllib.parse import urlsplit
                parts = urlsplit(str(_cfg_get(self._config, "server_url", _DEFAULT_SERVER_URL)))
                mcp_url = f"{parts.scheme}://{parts.hostname or '127.0.0.1'}:{_DEFAULT_MCP_PORT}/mcp"
            # mcp_token_file is independent of the HTTP token (separate faces):
            # defaulting it to token_file sends the WRONG credential class and 401s.
            token_file = str(_cfg_get(self._config, "mcp_token_file", ""))
            token = ""
            if token_file:
                try:
                    token = open(os.path.expanduser(token_file), encoding="utf-8").read().strip()
                except OSError:
                    token = ""
            client = mcp_client.McpClient(mcp_url, token=token, timeout=10.0)
            client.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": "hermes-semantic-memory", "version": "1.0"}})
            self._mcp = client
        except Exception as exc:  # noqa: BLE001 - routing is optional (I1)
            logger.debug("MCP routing unavailable (%s); flat search only", exc)
            self._mcp = None
        return self._mcp

    def _overlap_rerank(self, query: str, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Client-side relevance heuristic (documented; NOT an LLM rerank): stable sort
        by term-overlap with the query. Server-side LLM rerank is used when a capable
        tool exists in a future server build."""
        q_terms = {t for t in query.lower().split() if len(t) > 2}

        def _score(r: Dict[str, Any]) -> float:
            text = ((r.get("content") or "") + " " + (r.get("namespace") or "")).lower()
            return sum(1 for t in q_terms if t in text)

        return sorted(results, key=lambda r: (_score(r), float(r.get("score") or 0)), reverse=True)

    def _search(self, query: str) -> List[Dict[str, Any]]:
        namespaces = self._config.get("namespaces") or []
        # Routed path: complex query classes use the server's routing tool via MCP.
        query_class = classify_query(query)
        mcp = self._mcp_client() if query_class in _ROUTED_CLASSES else None
        if mcp is not None:
            try:
                data = mcp.tool_call("sm_search_with_routing",
                                     {"query": query, "top_k": self._max_facts() * 2,
                                      "query_class": query_class,
                                      "namespaces": list(namespaces)})
                results = mcp_client_unwrap(data)
                if query_class in ("C", "D") and len(results) > 2:
                    results = self._overlap_rerank(query, results)
                if results:
                    return results
                # server returned nothing routed; fall through to flat
            except Exception as exc:  # noqa: BLE001 - I1
                logger.debug("routed search failed (%s); flat fallback", exc)
                self._mcp = None
        payload: Dict[str, Any] = {"query": query, "top_k": self._max_facts() * 2}
        if namespaces:
            payload["namespaces"] = list(namespaces)
        data = self._request(_SEARCH_ROUTE, payload)
        items = data if isinstance(data, list) else (data or {}).get("results", [])
        return [r for r in items if isinstance(r, dict)]

    def _record_outcome(self, query: str, query_class: str, kept: list) -> None:
        """Fire-and-forget RL-routing feedback via POST /record-outcome (fail-open).
        Good result = at least one quality-gated fact (operator-proven rule)."""
        try:
            self._request("/record-outcome", {
                "query": (query or "")[:200],
                "outcome": "good" if kept else "bad",
                "query_class": query_class,
            }, timeout=3.0)
        except Exception:  # noqa: BLE001 - I1
            pass

    def _max_facts(self) -> int:
        try:
            return max(1, min(10, int(_cfg_get(self._config, "max_facts", 5))))
        except (TypeError, ValueError):
            return 5

    # -- Capture (opt-in; see capture.py) ----------------------------------------

    def _mcp_factory_for_capture(self) -> Any:
        """Factory for the CaptureQueue: returns the routed MCP client or None."""
        try:
            mcp = self._mcp_client()
            if mcp is None:
                return None
            # wrapper exposing tool_call directly (mcp_client.McpClient already has it)
            return mcp
        except Exception:  # noqa: BLE001 - I1
            return None

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None,
                  turn_author: Optional[Dict[str, Any]] = None) -> None:
        if self._capture is not None:
            self._capture.sync_turn(user_content or "", assistant_content or "",
                                    session_id or self._session_id)

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
            {"key": "ledger_enabled", "description": "Write per-turn injection outcome ledger (recommended)", "default": "true"},
            {"key": "ledger_path", "description": "Ledger JSONL path (default: <HERMES_HOME>/semantic-memory/injection-ledger.jsonl)", "default": ""},
            {"key": "mcp_url", "description": "MCP HTTP face URL (default: server host, port 17440)", "default": ""},
            {"key": "mcp_token_file", "description": "MCP bearer token file (defaults to token_file)", "default": ""},
            {"key": "routed_search", "description": "Use MCP routing for complex query classes (recommended)", "default": "true"},
            {"key": "capture_enabled", "description": "Capture durable user statements via sm_add_fact (opt-in)", "default": "false"},
            {"key": "capture_flush_turns", "description": "Flush captured statements every N turns", "default": "4"},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        from hermes_cli.config import save_config
        save_config({"plugins": {"semantic-memory": dict(values)}}, merge_existing=True)

    def post_setup(self, hermes_home: str, config: Dict[str, Any]) -> None:
        """Setup-wizard hook: collect connection settings, activate, verify honestly."""
        from hermes_cli.config import save_config
        from hermes_cli.memory_setup import _prompt
        print("\n  Configuring semantic-memory:\n")
        current_url = str(_cfg_get(self._config, "server_url", _DEFAULT_SERVER_URL))
        url = _prompt("Server URL", default=current_url)
        current_token = str(_cfg_get(self._config, "token_file", ""))
        token_file = _prompt("Token file path (blank for a loopback server without auth)",
                             default=current_token)
        memory = config["memory"] = config["memory"] if isinstance(config.get("memory"), dict) else {}
        memory["provider"] = self.name
        memory[self.name] = {"server_url": url, "token_file": token_file}
        self._config.update({"server_url": url, "token_file": token_file})
        try:
            save_config(config)
            print("  Activation and connection settings saved to config.yaml "
                  "(memory.semantic-memory).")
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: could not save config: {exc}")
            return
        reachable = self.is_available()
        if reachable:
            print("\n  semantic-memory reachable — provider activated. "
                  "Start a new session to use it.\n")
        else:
            print(
                "\n  semantic-memory server NOT reachable at "
                f"{url} ({self.unavailable_reason()}).\n"
                "  Settings are saved; recall stays idle until the server is running:\n"
                "    cargo install semantic-memory-mcp\n"
                "    semantic-memory-mcp --memory-dir ~/.semantic-memory --http-port 17441\n"
                "  (repo: github.com/RecursiveIntell/semantic-memory)\n"
            )

def register(ctx: Any) -> None:
    """Plugin entry point (plugin.yaml / entry-point discovery).

    Config resolution: the ``hermes memory setup`` contract stores provider settings
    under ``memory.semantic-memory``; the dashboard panel stores them under
    ``plugins.semantic-memory``. Setup wins on conflict; both are read so either
    configuration path works.
    """
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config import cfg_get

    try:
        cfg = load_config_readonly() or {}
    except Exception:
        cfg = {}
    config = dict(cfg_get(cfg, "plugins", "semantic-memory", default={}) or {})
    setup_cfg = cfg_get(cfg, "memory", "semantic-memory", default={}) or {}
    config.update(setup_cfg)
    ctx.register_memory_provider(SemanticMemoryProvider(config=config))