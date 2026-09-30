# Semantic Memory Provider for Hermes Agent

A [memory provider plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin) for
[Hermes Agent](https://github.com/NousResearch/hermes-agent) backed by a **local semantic-memory server**:
a knowledge store with embeddings (HNSW), full-text search, a knowledge graph, and per-fact provenance.

What you get, once selected via `hermes memory setup`:

- **Automatic recall injection** — relevant durable facts are injected into every turn with
  trust tiers, so the agent remembers without you pasting context or running searches.
- **Evidence-gated recall** — injection passes an evidence quality gate: template artifacts,
  prompt-pack scaffolding, stale "done/shipped" claims, and speculation are filtered out;
  durable facts keep with a cited namespace. The injected block tells the model which parts
  are trustworthy and which still require live verification.
- **`sm_search` tool** — the agent can deep-search memory on demand (provenance-aware).

## Install

1. Run the memory server (see [Server](#server) below) and note its URL + token file.
2. Copy or symlink this provider into your Hermes plugin dir:

   ```bash
   mkdir -p ~/.hermes/plugins
   cp -r plugin-provider/semantic-memory ~/.hermes/plugins/semantic-memory
   ```

3. Launch `hermes memory setup`, pick **Semantic Memory**, and enter the server URL /
   token file when prompted. Activation is written to `config.yaml`
   (`memory.provider: semantic-memory`); start a new session.

No Hermes core changes are required — the provider implements the standard
`MemoryProvider` ABC and rides the normal discovery + setup flow.

## Server

The provider talks HTTP to the `semantic-memory-mcp` server binary
(repo: `RecursiveIntell/semantic-memory`, installable with `cargo install semantic-memory-mcp`).

Minimal launch (loopback, no auth):

```bash
semantic-memory-mcp --memory-dir ~/.semantic-memory --http-port 17441
```

With a bearer token:

```bash
semantic-memory-mcp --memory-dir ~/.semantic-memory --http-port 17441 \
  --http-auth-token-file ~/.semantic-memory/token
```

Then set `token_file: ~/.semantic-memory/token` in the provider config (dashboard panel
or `hermes memory setup`). The provider **fails open**: if the server is down, turns
proceed normally without recall, and capture retries on later turns.

## Config

Stored under `plugins.semantic-memory` in `config.yaml` (editable via the dashboard panel):

| Key | Default | Description |
|---|---|---|
| `server_url` | `http://127.0.0.1:17441` | Server HTTP endpoint. |
| `token_file` | *(empty)* | File containing the bearer token. |
| `max_facts` | `5` | Max facts injected per turn (1–10). |
| `capture_enabled` | *(reserved)* | Turn capture ships in a future release; recall-only today. |

## Trust tiers (what the injected header means)

Injected recall is labeled per fact:

- `authoritative_durable` — verification-backed fact in a trusted namespace; usable directly.
- `durable_structural` — clean structural/architecture fact in a trusted namespace; usable with the namespace cited.
- `background` / `speculative` — hints only; verify before use.
- Current-state questions ("is X shipped?") additionally carry a live-verification reminder:
  memory is *discovery, not proof*; the agent verifies against repos/config/processes before
  making claims.

Namespaces starting with `quarantine-` / `archive-` are excluded from recall — curation
moves facts there instead of deleting them.

## Tests

```bash
python3 tests/test_recall_gate.py          # stdlib-only, no runtime needed
# or, with the hermes-agent repo on PYTHONPATH:
pytest tests/ -q
```

## License

MIT