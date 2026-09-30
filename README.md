# Semantic Memory Provider for Hermes Agent

A community [memory provider plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin) for
[Hermes Agent](https://github.com/NousResearch/hermes-agent) that retrieves facts from a
separately run **semantic-memory** HTTP server: a knowledge store with embeddings (HNSW),
full-text search, a knowledge graph, and per-fact provenance
([crates.io: semantic-memory-mcp](https://crates.io/crates/semantic-memory-mcp),
repo: `RecursiveIntell/semantic-memory`).

What it does, once selected via `hermes memory setup`:

- **Recall injection** — on eligible turns (non-trivial prompts), the provider searches the
  memory server and injects matching facts above the user message, each labeled by a
  heuristic evidence gate. If the server is unreachable or nothing matches, the turn
  proceeds normally — recall fails open to no injection.
- **Heuristic evidence gate** — injection drops results matching template-artifact markers,
  prompt-pack scaffolding, and speculative language; stale "done/shipped" phrasing is
  dropped when the query asks about current status. Facts in trusted namespaces keep by
  default with the namespace cited. **These are keyword/namespace heuristics, not trust
  guarantees**: retrieved memory is untrusted *data*, never proof or instructions, and a
  stale or malicious entry in an allowlisted namespace can pass the gate.
- **Injected header sets reading rules** — labels are heuristic quality hints; the header
  tells the agent that current-state claims require live verification against
  repos/config/processes, and that recalled content is not proof.
- **`sm_search` tool** — the agent can search memory on demand; results come back labeled
  (IDs, namespaces, scores, heuristic quality labels) with a safety note when unsafe-labeled
  results are present.
- **Routed search** — complex queries (multi-hop, contradiction, synthesis, temporal) use the
  server's routing surface (MCP) with a local relevance rerank; flat search for simple lookups;
  falls back to flat automatically when MCP is unreachable.
- **Injection ledger** — when enabled (default), the provider appends a JSONL record for
  eligible recall turns whose search returned results (kept vs filtered label counts) so
  recall yield is measurable over time.
- **Probe CLI** — `python3 -m probe` (with the runtime python) runs a fixed 20-query set against
  your install and reports the durable-hit yield.
- **Turn capture (opt-in)** — capture of qualifying user utterances: original text,
  unchanged and truncated to 400 characters, is sent through the MCP `sm_add_fact` path.
  This provider does not verify the server receipt. Eligibility is built-in: a final
  question mark and a small fixed list of acknowledgments are excluded, and otherwise one
  of the built-in regex patterns must match ("I prefer/like/want…", "the user
  prefers/decided…", similar) — phrase matching does not assess durability or truth, and
  the patterns are not user-configurable in this release. Off by default; requires the
  server to run with an operator-authority token and a full tool profile.

## Install

1. Run the memory server (see [Server](#server) below) and note its URL + token file.
2. Copy this provider into your Hermes plugin dir:

   ```bash
   mkdir -p ~/.hermes/plugins
   cp -r plugin-provider/semantic-memory ~/.hermes/plugins/semantic-memory
   ```

3. Run `hermes memory setup`, pick **Semantic Memory**, and enter the server URL and
   token-file path when prompted (blank token path for a loopback server without auth).
   Activation is written to `config.yaml` under `memory.semantic-memory`; start a new session.
   The dashboard panel can edit the same fields later (stored under `plugins.semantic-memory`).

No Hermes core changes are required — the provider implements the standard
`MemoryProvider` ABC and rides the normal discovery + setup flow.

## Server

The provider talks HTTP to the `semantic-memory-mcp` server binary (a separate project,
not part of this repo). One quirk to know: the HTTP face lives alongside the server's
stdio MCP loop, so for a long-running HTTP server you should also enable the MCP HTTP
port (`--mcp-http-port`) — otherwise the process exits when its stdin closes. The
simplest durable setup is a systemd user unit:

```bash
cargo install semantic-memory-mcp

# create a store and a token (server REQUIRES HTTP auth; it refuses to serve without one):
mkdir -p ~/.semantic-memory
python3 -c "import secrets; open('/home/YOU/.semantic-memory/.token','w').write(secrets.token_urlsafe(32))"
chmod 600 ~/.semantic-memory/.token

# one-shot foreground run (dies when the shell/stdin closes — fine for a quick try):
semantic-memory-mcp --memory-dir ~/.semantic-memory \
  --http-port 17441 --http-auth-token-file ~/.semantic-memory/.token \
  --mcp-http-port 17440 \
  --mcp-http-token-file ~/.semantic-memory/.token

# durable run (recommended) — ~/.config/systemd/user/semantic-memory.service:
#   [Unit]
#   Description=Semantic Memory server
#   [Service]
#   Type=simple
#   ExecStart=%h/.local/bin/semantic-memory-mcp --memory-dir %h/.semantic-memory --http-port 17441 --http-auth-token-file %h/.semantic-memory/.token --mcp-http-port 17440 --mcp-http-token-file %h/.semantic-memory/.token
#   Environment=RUST_LOG=info
#   [Install]
#   WantedBy=default.target
# then: systemctl --user daemon-reload && systemctl --user enable --now semantic-memory
```

Then set `token_file` in the provider config (setup prompt or dashboard panel). The
provider **fails open**: if the server is down, turns proceed normally without recall.

Server-side security (auth binding, exposure) is the server project's domain — do not
expose an unauthenticated instance beyond loopback. Its behavior was not security-reviewed
for this README.

## Config

Setup stores connection settings under `memory.semantic-memory` in `config.yaml`; the
dashboard panel stores them under `plugins.semantic-memory`. Both are read (setup wins).

| Key | Default | Description |
|---|---|---|
| `server_url` | `http://127.0.0.1:17441` | Server HTTP endpoint. |
| `token_file` | *(empty)* | File containing the bearer token. Blank = no auth header. |
| `max_facts` | `5` | Max facts injected per turn (1–10). |
| `namespaces` | *(empty)* | Optional recall namespace filter list. |
| `ledger_enabled` | `true` | Per-turn injection outcome ledger (JSONL). |
| `ledger_path` | `<HERMES_HOME>/semantic-memory/injection-ledger.jsonl` | Ledger location. |
| `routed_search` | `true` | MCP routing for complex query classes; flat fallback. |
| `mcp_url` / `mcp_token_file` | host :17440 / *(empty)* | MCP face URL; token file is independent of the HTTP token (different faces). |
| `capture_enabled` | `false` | Opt-in durable-statement capture (needs operator-authority token + full profile on the server). |
| `capture_flush_turns` | `4` | Batch size for capture flush. |

## Recall labels (heuristic hints, not guarantees)

- `authoritative_durable` — verification-flavored keywords present and namespace trusted.
- `durable_structural` — clean content in a trusted namespace.
- `background` / `speculative` — hints only; verify before use.
- `artifact_template` / `stale_status` — never injected; shown in a filtered-count note.

The gate may miss unsafe or stale content that doesn't match its patterns; it also may
filter useful content. Passing the gate does not make content true — verify claims that
matter against live sources.

## Tests

```bash
python3 tests/test_recall_gate.py          # stdlib-only, no runtime needed
# or, with the hermes-agent repo on PYTHONPATH:
pytest tests/ -q
```

## Status & limits

v0.2: recall injection, routed search, injection ledger, probe CLI, and opt-in turn
capture. Capture routes pattern-matched user utterances (unedited, truncated) through the
governed `sm_add_fact` path; the server receipt carries provenance for what is stored.
The evidence gate is covered by targeted tests only — there is no claim of general
recall accuracy, adversarial robustness, or prompt-injection resistance. Compaction
preservation of injected recall depends on the runtime's context engine (Hermes
built-in compressor does not preserve recall blocks; the Ares context-governor does,
outside this plugin's scope).

## License

MIT