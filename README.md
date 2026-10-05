# Semantic Memory Provider for Hermes Agent

A community [memory provider plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin) for
[Hermes Agent](https://github.com/NousResearch/hermes-agent) that retrieves facts from a
separately run **semantic-memory-mcp** HTTP server: a knowledge store with embeddings (HNSW),
full-text search, a knowledge graph, and per-fact provenance
([crates.io: semantic-memory-mcp](https://crates.io/crates/semantic-memory-mcp),
repo: [`RecursiveIntell/semantic-memory-mcp`](https://github.com/RecursiveIntell/semantic-memory-mcp)).

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
- **Probe CLI** — from `plugin-provider/semantic-memory/` (or its installed
  plugin directory), `python3 -m probe` with the runtime Python runs a fixed
  20-query set against the configured provider and reports heuristic recall
  yield. The probe invokes the real provider path, including enabled ledger
  writes and outcome feedback; it is not a side-effect-free inspection.
- **Turn capture (opt-in)** — capture of qualifying user utterances: original text,
  unchanged and truncated to 400 characters, is sent through the MCP `sm_add_fact` path.
  This provider does not verify the server receipt. Eligibility is built-in: a final
  question mark and a small fixed list of acknowledgments are excluded, and otherwise one
  of the built-in regex patterns must match ("I prefer/like/want…", "the user
  prefers/decided…", similar) — phrase matching does not assess durability or truth, and
  the patterns are not user-configurable in this release. Off by default; capture requires a server-configured operator-authority token (distinct from the provider's HTTP/MCP bearer token) and a profile exposing `sm_add_fact`. Builds with the updated allowlist include it in `agent`; the GitHub `v1.1.0` tag (crate `0.5.6`) omits it from `agent`, so use `full` with that tag or upgrade to a build with the updated `agent` allowlist.

## Install

1. Run the memory server (see [Server](#server) below) and note its URL + token file.
2. Copy this provider into your Hermes plugin dir:

   ```bash
   mkdir -p ~/.hermes/plugins
   cp -r plugin-provider/semantic-memory ~/.hermes/plugins/semantic-memory
   ```

3. Run `hermes memory setup`, pick **Semantic Memory**, and enter the server URL and
   configured bearer-token file path when prompted. The current server requires HTTP authentication, including on loopback.
   Activation is written to `config.yaml` under `memory.semantic-memory`; start a new session.
   The dashboard panel can edit the same fields later (stored under `plugins.semantic-memory`).

No Hermes core changes are required — the provider implements the standard
`MemoryProvider` ABC and rides the normal discovery + setup flow.

## Server

The provider talks HTTP to the `semantic-memory-mcp` server binary and crate in the separate [semantic-memory-mcp repository](https://github.com/RecursiveIntell/semantic-memory-mcp).

The transport behavior here describes [current GitHub source at `ec0b3fd`](https://github.com/RecursiveIntell/semantic-memory-mcp/blob/ec0b3fda093128e0da69e49c99cd396b7fc949fe/src/main.rs#L364-L445), with HTTP transports enabled by its build features. Verify the actual registry-installed version's `--help` before using these options:

- `--mcp-http-port` enables the MCP transport needed for routed search and keeps the process running without stdin.
- `--http-only` keeps the auxiliary HTTP server running without stdio MCP; it does not enable routed MCP search by itself.
- The stdio-only mode exits when its input closes. Both HTTP faces require configured bearer tokens.

A systemd user unit can supervise a durable server:

Token creation below uses the current home, creates the file with mode `0600`, and refuses an existing `.token` file. To reuse an existing configured private token, skip the creation block and keep its token-file path; do not overwrite it.

```bash
cargo install semantic-memory-mcp

# create a store and a token (server REQUIRES HTTP auth; it refuses to serve without one):
mkdir -p ~/.semantic-memory
python3 - <<'PY'
import os
import secrets
from pathlib import Path

token_path = Path.home() / ".semantic-memory" / ".token"
fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as token_file:
    token_file.write(secrets.token_urlsafe(32))
PY
chmod 600 ~/.semantic-memory/.token

# foreground run (Ctrl-C stops it; MCP HTTP remains running if stdin closes):
semantic-memory-mcp --memory-dir ~/.semantic-memory \
  --http-port 17441 --http-auth-token-file ~/.semantic-memory/.token \
  --mcp-http-port 17440 \
  --mcp-http-token-file ~/.semantic-memory/.token

# durable run (recommended) — ~/.config/systemd/user/semantic-memory.service:
#   [Unit]
#   Description=Semantic Memory server
#   [Service]
#   Type=simple
#   ExecStart=%h/.cargo/bin/semantic-memory-mcp --memory-dir %h/.semantic-memory --http-port 17441 --http-auth-token-file %h/.semantic-memory/.token --mcp-http-port 17440 --mcp-http-token-file %h/.semantic-memory/.token
#   Environment=RUST_LOG=info
#   [Install]
#   WantedBy=default.target
# then: systemctl --user daemon-reload && systemctl --user enable --now semantic-memory
```

The unit uses the default `cargo install` binary location, `%h/.cargo/bin/semantic-memory-mcp`. For a custom installation, use the path returned by `command -v semantic-memory-mcp` in `ExecStart`.

Then set `token_file` in the provider config (setup prompt or dashboard panel). The
provider **fails open**: if the server is down, turns proceed normally without recall.

Server-side security (auth binding, exposure) is the server project's domain. Configure the required bearer-token files and review bind/exposure settings before startup. This README source review does not qualify a running service or an installed release.

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
| `capture_enabled` | `false` | Opt-in capture of pattern-matched user utterances; requires a server operator-authority token and a profile exposing `sm_add_fact` (`agent` on updated allowlists; `full` on the GitHub `v1.1.0` tag, crate `0.5.6`), plus `routed_search=true` because capture uses the same MCP client factory. |
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
governed `sm_add_fact` path; the provider does not verify server receipts.
The evidence gate is covered by targeted tests only — there is no claim of general
recall accuracy, adversarial robustness, or prompt-injection resistance. Compaction
preservation of injected recall depends on the runtime's context engine (Hermes
built-in compressor does not preserve recall blocks; the Ares context-governor does,
outside this plugin's scope).

## License

MIT