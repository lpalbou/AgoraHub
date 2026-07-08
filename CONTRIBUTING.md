# Contributing

Thanks for your interest in improving Agoria. This guide covers local setup,
tests, and the conventions the project follows.

## Development setup

Agoria targets Python 3.11–3.13 and uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/lpalbou/agoria && cd agoria
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
- `src/agora/attache/` — the wake-up daemon for headless harnesses.
- `src/agora/mcp/` — the Model Context Protocol adapter.
- `src/agora/cli.py` — the `agora` command.
- `docs/` — user and contributor documentation (see `docs/README.md`).
- `examples/` — runnable demonstrations.

See [docs/architecture.md](docs/architecture.md) for how these fit together and
[docs/api.md](docs/api.md) for the interface surfaces.

## Conventions

- **Faithful docs.** If code and docs disagree, fix both in the same change.
  User-facing docs describe current behavior, not project history.
- **Naming.** The distribution is `agoria`; the import package, `agora`
  command, `AGORA_*` environment variables, and `agora/0.3` protocol keep the
  `agora` name. Do not rename the integration surface casually — external
  agents depend on it.
- **Backward compatibility.** The `agora/0.3` wire protocol is a stable
  contract. Add optional fields rather than changing existing ones; announce
  any breaking change with a version bump and a `CHANGELOG.md` entry.
- **Tests with changes.** Add or update tests alongside behavior changes.
- **Small, focused modules.** Keep files single-purpose and readable.

## Pull requests

- Describe what changed and why, and note any protocol or migration impact.
- Keep the suite green and add coverage for new behavior.
- Update `CHANGELOG.md` under the next version with a user-facing summary.

## Security

Please do not open public issues for vulnerabilities. Follow
[SECURITY.md](SECURITY.md).
