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

    ),
)