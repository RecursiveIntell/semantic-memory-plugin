"""Declarative dashboard config schema for the semantic-memory provider.

Loaded by path by the web server (never imported as a package), so this file
must import only the pure schema module from plugins/memory/config_schema.py.
Field defaults are strings per the generic renderer's convention.
"""
from __future__ import annotations

from plugins.memory.config_schema import (
    KIND_NUMBER,
    KIND_TEXT,
    ProviderConfigSchema,
    ProviderField,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="semantic-memory",
    label="Semantic Memory",
    fields=(
        ProviderField(
            key="server_url",
            label="Server URL",
            kind=KIND_TEXT,
            description="HTTP endpoint of the running semantic-memory server",
            default="http://127.0.0.1:17441",
        ),
        ProviderField(
            key="token_file",
            label="Token file",
            kind=KIND_TEXT,
            description="File containing the server bearer token (blank when the server is open on loopback)",
            default="",
        ),
        ProviderField(
            key="max_facts",
            label="Max facts per turn",
            kind=KIND_NUMBER,
            description="Maximum facts injected into a turn (1-10)",
            default="5",
        ),
        ProviderField(
            key="ledger_enabled",
            label="Injection ledger",
            kind=KIND_BOOL,
            description="Write per-turn injection outcome records (JSONL) for measurability",
            default="true",
        ),
        ProviderField(
            key="routed_search",
            label="Routed search",
            kind=KIND_BOOL,
            description="Use the MCP routing surface for complex query classes (flat fallback)",
            default="true",
        ),
        ProviderField(
            key="mcp_token_file",
            label="MCP token file",
            kind=KIND_TEXT,
            description="Token file for the MCP face (independent of the HTTP token)",
            default="",
        ),
        ProviderField(
            key="capture_enabled",
            label="Turn capture",
            kind=KIND_BOOL,
            description="Store pattern-matched user utterances via sm_add_fact (requires operator-authority token on the server)",
            default="false",
        ),
        ProviderField(
            key="mcp_url",
            label="MCP URL",
            kind=KIND_TEXT,
            description="MCP face URL (default: server host, port 17440)",
            default="",
        ),

    ),
)