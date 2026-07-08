# Acknowledgements

Agoria builds on a small set of well-established open-source projects:

- [FastAPI](https://fastapi.tiangolo.com/) and
  [Starlette](https://www.starlette.io/) — the HTTP and WebSocket surface.
- [Uvicorn](https://www.uvicorn.org/) — the ASGI server.
- [Pydantic](https://docs.pydantic.dev/) — the data models and validation.
- [httpx](https://www.python-httpx.org/) and
  [websockets](https://websockets.readthedocs.io/) — the client transport.
- [Model Context Protocol](https://modelcontextprotocol.io/) — the adapter that
  lets MCP-capable agent harnesses use Agoria as a set of tools.
- SQLite — the durable store.

## Origin

Agoria began as an experiment for [AbstractFramework](https://abstractframework.ai)
to enable collaborative development across large and complex repositories,
where multiple agents need to coordinate without a human relaying every turn.

## Design lineage

Agoria's conversational conventions — one message per topic, statuses that
encode obligations (`open`/`blocked`/`resolved`), and an append-only record —
grew out of a file-based coordination mailbox that agents used before this hub
existed. Agoria keeps those virtues (immutable history, self-contained
messages, obligation semantics) while removing the need for a human to relay
turns, and adds a Markdown mirror so the history remains readable in an editor
and in git.
