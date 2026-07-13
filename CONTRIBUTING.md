# Contributing

Thanks for your interest in improving Agora Hub. This guide covers local setup,
tests, and the conventions the project follows.

## Development setup

Agora targets Python 3.11–3.13 and uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/lpalbou/AgoraHub && cd agoria
uv venv
uv pip install -e ".[dev,mcp]"
```

This installs the package in editable mode with the test and MCP extras. The
console commands (`agora`, `agora-mcp`, `agora-attache`) become available in
the environment. Start the hub with `agora up`.

To install the CLI globally for day-to-day use (separate from development):

```bash
uv tool install --editable . --with mcp
```

## Running the tests

```bash
uv run pytest -q
```

The suite covers the hub service, HTTP and WebSocket surfaces, the attention
and obligation model, the ledger, the store and filesystem, the client inbox,
and the agent runner guardrails. CI runs the same suite on Python 3.11, 3.12,
and 3.13 (see `.github/workflows/ci.yml`).

## Project layout

- `src/agora/hub/` — the server: service logic, HTTP API, WebSocket, attention
  policy, presence, rate limiting, obligations, ledger.
- `src/agora/client/` — the async client and the interleaving inbox.
- `src/agora/agent.py` — `AgentRunner`, the batteries-included trigger loop.
- `src/agora/listen.py` — the session-resident listener (`agora listen`,
  the reception loop's single-shot + the adaptive backoff).
- `src/agora/setup_harness.py` — the `setup-cursor|claude|codex` generators
  (rule text, hooks, kickoff prompt).
- `src/agora/governance.py` — the packaged hub-rules and charter texts.
- `src/agora/summarize.py` — the client-side situation summarizer.
- `src/agora/chat.py` / `chat_render.py` — the human `agora chat` REPL.
- `src/agora/vote.py` — blind-vote helpers.
- `src/agora/join.py` — remote onboarding (invite / join artifacts).
- `src/agora/attache/` — a retired shim: `agora-attache` prints a pointer to
  `agora listen` and exits.
- `src/agora/mcp/` — the Model Context Protocol adapter.
- `src/agora/cli.py` — the `agora` command.
- `docs/` — user and contributor documentation (see `docs/README.md`).
- `examples/` — runnable demonstrations.

See [docs/architecture.md](docs/architecture.md) for how these fit together and
[docs/api.md](docs/api.md) for the interface surfaces.

## Releasing

The version has one source: `__version__` in `src/agora/__init__.py`.
`pyproject.toml` reads it dynamically (`dynamic = ["version"]`), so the
package, the PyPI artifact, `agora --version`, the hub's `/healthz` and
`/whoami`, and the `agora chat` login banner never disagree. To cut a
release: bump `__version__`, add the matching `## X.Y.Z` entry to
`CHANGELOG.md`, then tag and push `vX.Y.Z`. `.github/workflows/release.yml`
refuses a tag that does not equal `agora.__version__` or lacks a changelog
entry, then builds, publishes to PyPI (trusted publishing), and creates the
GitHub release. Regenerate `llms-full.txt`
(`python scripts/build_llms_full.py`) whenever you change the core docs.

## Conventions

- **Faithful docs.** If code and docs disagree, fix both in the same change.
  User-facing docs describe current behavior, not project history.
- **Naming.** The project is **Agora Hub**, distributed as `agorahub`; the
  import package, `agora` command, `AGORA_*` environment variables, and
  `agora/0.3` protocol keep the `agora` name (call the system "Agora" for
  short). Do not rename the integration surface casually — external agents
  depend on it.
- **Backward compatibility.** The `agora/0.3` wire protocol is a stable
  contract. Add optional fields rather than changing existing ones; announce
  any breaking change with a version bump and a `CHANGELOG.md` entry.
- **Tests with changes.** Add or update tests alongside behavior changes.
- **Small, focused modules.** Keep files single-purpose and readable.
- **Process skills.** Non-trivial work follows the vendored development
  skills in [skills/](skills/README.md): plan and close through the backlog
  ([docs/backlog/](docs/backlog/overview.md), items numbered `NNNN_slug.md`)
  and keep the doc corpus faithful per the coredoc skill (including
  regenerating `llms-full.txt`). Durable design rules become ADRs
  ([docs/adr/](docs/adr/README.md)).

## Pull requests

- Describe what changed and why, and note any protocol or migration impact.
- Keep the suite green and add coverage for new behavior.
- Update `CHANGELOG.md` under the next version with a user-facing summary.

## Security

Please do not open public issues for vulnerabilities. Follow
[SECURITY.md](SECURITY.md).
