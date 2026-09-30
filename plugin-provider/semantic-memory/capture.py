#!/usr/bin/env python3
"""Opt-in turn capture: qualifying user utterances -> MCP sm_add_fact.

Conservative by construction: capture stores the ORIGINAL user text (truncated
to 400 chars) only when a built-in pattern matches — never transcripts.
Statements are recognized by
first-person/user-reference patterns on the user side of a completed turn;
everything else is ignored. Provenance: namespace 'conversation', source
'turn-capture'. Fail-open (I1): an unreachable MCP surface queues (bounded) —
a later successful flush drains it; nothing ever blocks a turn.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_QUEUE = 200

# Durable statement patterns (user side): first-person preference/decision/reference.
_EXTRACT_PATTERNS = (
    re.compile(r"^\s*i\s+(?:prefer|like|love|use|want|need|decided|decide|chose|choose|always|never)\b(.{5,})", re.IGNORECASE),
    re.compile(r"\bthe user\s+(?:prefers|likes|uses|wants|needs|decided|chose)\b(.{5,})", re.IGNORECASE),
    re.compile(r"^\s*(?:my|the)\s+(?:favorite|preferred|default)\s+\w+\s+is\s+(.{5,})", re.IGNORECASE),
    re.compile(r"^\s*(?:we|the team)\s+(?:use|standardized on|decided)\s+(.{5,})", re.IGNORECASE),
)

_QUESTION_RE = re.compile(r"\?\s*$")


def extract_candidates(user_content: str, assistant_content: str) -> List[Dict[str, Any]]:
    """Return the original user text (truncated to 400 chars) when it matches a
    built-in pattern; [] otherwise. This does not assess durability or truth."""
    user = (user_content or "").strip()
    if not user or _QUESTION_RE.search(user):
        return []
    low = user.lower()
    if low in ("ok", "ok thanks", "thanks", "sure", "yes", "no", "got it",
               "sounds good", "that works", "continue", "go on", "next"):
        return []
    cands: List[Dict[str, Any]] = []
    for pattern in _EXTRACT_PATTERNS:
        m = pattern.search(user)
        if m:
            text = user if m.start() == 0 else user
            text = text[:400]
            cands.append({
                "content": text,
                "namespace": "conversation",
                "source": "turn-capture",
                "content_hash": hashlib.sha256(text.lower().encode()).hexdigest()[:16],
            })
            break
    return cands


class CaptureQueue:
    """Queues pattern-matched user utterances and flushes them through MCP
    sm_add_fact. No durability or truth assessment is performed."""

    def __init__(self, mcp_factory: Callable[[], Any], *,
                 flush_turns: int = 4, queue_path: Optional[str] = None):
        self._mcp_factory = mcp_factory
        self._flush_every = max(1, int(flush_turns))
        self._lock = threading.Lock()
        self._queue: List[Dict[str, Any]] = []
        self._turns_since_flush = 0
        self._seen: set = set()
        self._queue_path = queue_path  # future: optional crash-survivable queue

    def sync_turn(self, user_content: str, assistant_content: str, session_id: str = "") -> int:
        """Extract + queue one turn. Returns number flushed. Never raises."""
        try:
            with self._lock:
                self._turns_since_flush += 1
                for cand in extract_candidates(user_content, assistant_content):
                    if cand["content_hash"] in self._seen:
                        continue
                    self._seen.add(cand["content_hash"])
                    cand["session_id"] = session_id
                    self._queue.append(cand)
                    if len(self._queue) > _MAX_QUEUE:
                        self._queue.pop(0)
                if self._turns_since_flush >= self._flush_every:
                    return self._flush_locked()
            return 0
        except Exception:
            return 0

    def _flush_locked(self) -> int:
        self._turns_since_flush = 0
        if not self._queue:
            return 0
        batch = list(self._queue)
        stored = 0
        try:
            mcp = self._mcp_factory()
            if mcp is None:
                return 0
            for cand in batch:
                try:
                    # NOTE: no 'source' argument — the governed admission gate treats
                    # source references as external evidence requiring a trusted
                    # immutable-object resolver (verified: passing one fails closed).
                    # Provenance lives in the server's receipt, not client claims.
                    mcp.tool_call("sm_add_fact", {
                        "content": cand["content"],
                        "namespace": cand.get("namespace", "conversation"),
                    })
                    stored += 1
                    self._queue.remove(cand)
                except Exception as exc:  # noqa: BLE001 - keep unflushed, retry later
                    logger.debug("capture deferred: %s", exc)
                    break
        except Exception as exc:  # noqa: BLE001 - server down: re-held
            logger.debug("capture flush unavailable: %s", exc)
        return stored

    def flush(self) -> int:
        with self._lock:
            return self._flush_locked()

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)