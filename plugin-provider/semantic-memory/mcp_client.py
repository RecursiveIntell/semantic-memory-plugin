#!/usr/bin/env python3
"""Minimal MCP streamable-HTTP client for the semantic-memory server.

Contract verified against server 0.5.6/0.5.8 (operator + third-party deployments):
- Accept must include 'application/json, text/event-stream' (406 otherwise)
- the server returns no Mcp-Session-Id; 'notifications/initialized' is rejected
  (-32601) — send initialize (or not) then call methods directly
- responses are plain JSON or SSE (one data: line per JSON-RPC response)
Fail-open: callers wrap calls in try/except (invariant I1).
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, Optional

_urlopen = urllib.request.urlopen  # module-level for test injection


def rpc_body(method: str, params: Optional[Dict[str, Any]], _id: int = 1) -> Dict[str, Any]:
    body: Dict[str, Any] = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def parse_response(raw: str) -> Any:
    """Parse a plain-JSON or SSE JSON-RPC response; return .result (raise on .error)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        obj = json.loads(raw)
    else:
        obj = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                obj = json.loads(line[5:].strip())
                break
    if not isinstance(obj, dict):
        return obj
    if "error" in obj:
        raise RuntimeError(f"mcp error: {obj['error']}")
    return obj.get("result", obj)


def unwrap_results(result: Any) -> list:
    """Extract search results from an MCP tools/call result (defensive across builds)."""
    if result is None:
        return []
    # direct payload form
    direct = result if isinstance(result, list) else (result.get("results") if isinstance(result, dict) else None)
    if isinstance(direct, list):
        return [r for r in direct if isinstance(r, dict)]
    # wrapped form: {"content": [{"type": "text", "text": "<json payload>"}]}
    try:
        for block in result.get("content", []) or []:
            if block.get("type") == "text":
                payload = json.loads(block.get("text", ""))
                items = payload if isinstance(payload, list) else payload.get("results", [])
                if isinstance(items, list):
                    return [r for r in items if isinstance(r, dict)]
    except Exception:
        pass
    return []


class McpClient:
    """Tiny JSON-RPC client over streamable HTTP. No session IDs (0.5.x contract)."""

    def __init__(self, url: str, token: str = "", timeout: float = 12.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._next_id = 0

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self._next_id += 1
        body = json.dumps(rpc_body(method, params, self._next_id)).encode()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with _urlopen(req, timeout=self.timeout) as resp:
            return parse_response(resp.read().decode())

    def tool_call(self, name: str, arguments: Dict[str, Any]) -> Any:
        return self.call("tools/call", {"name": name, "arguments": arguments})