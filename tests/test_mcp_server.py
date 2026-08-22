import importlib
import sys
from pathlib import Path

import pytest


def _import_local_server(monkeypatch=None):
    src = Path(__file__).resolve().parents[1] / "src"
    if monkeypatch is not None:
        monkeypatch.syspath_prepend(str(src))
    elif str(src) not in sys.path:
        sys.path.insert(0, str(src))
    # Import a FRESH copy of the server WITHOUT leaving the rest of the
    # suite holding mismatched module objects: snapshot the loaded agora.*
    # modules, import fresh, then restore the snapshot. The old
    # pop-and-leave polluted class identity suite-wide — an exception
    # raised from the re-imported module escaped `pytest.raises` on the
    # original class in unrelated files.
    saved = {n: m for n, m in sys.modules.items()
             if n == "agora" or n.startswith("agora.")}
    for name in saved:
        sys.modules.pop(name, None)
    try:
        return importlib.import_module("agora.mcp.server")
    finally:
        for name in [n for n in sys.modules
                     if n == "agora" or n.startswith("agora.")]:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def test_tool_error_text_stringifies_error_dict():
    server = _import_local_server()
    out = server.tool_error_text({
        "ok": False,
        "error": 409,
        "detail": "read the charter first",
        "action": "REQUEST FAILED",
    })
    assert isinstance(out, str)
    assert '"ok": false' in out
    assert '"error": 409' in out
    assert '"detail": "read the charter first"' in out


def test_mcp_http_timeout_seconds_honors_env(monkeypatch):
    _import_local_server(monkeypatch)
    monkeypatch.setenv("AGORA_MCP_HTTP_TIMEOUT", "240")
    reloaded = _import_local_server(monkeypatch)
    assert reloaded.MCP_HTTP_TIMEOUT_SECONDS == 240.0


# -- the spawn tools (laurent dm#24): reachable, and pointed at the real wire

def _live_hub(tmp_path):
    """A real hub on an ephemeral port — same pattern as test_cli_surfaces.

    The MCP server holds a SYNC httpx client, so an in-process ASGI transport
    cannot serve it (ASGITransport is async-only). Running the hub for real
    also means the tools are exercised over the wire they actually use.
    """
    import socket
    import threading
    import time

    import uvicorn

    from agora.hub.app import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(db_path=str(tmp_path / "hub.db"), admin_key="k",
                     rate_per_minute=600.0)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test hub failed to start")
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}", server, thread


def _server_against(url: str, monkeypatch, agent_key: str):
    """Build the real MCP server pointed at a real hub. Everything above
    `_call` is exercised for real — the point is that a tool's ROUTE and
    payload are right, which is the half a hand-written stub would let
    drift."""
    server = _import_local_server(monkeypatch)
    monkeypatch.setattr(server, "_resolve_credentials",
                        lambda: (url, agent_key))
    return server.build_server()


def _call_tool(mcp, name: str, args: dict):
    """Decode a FastMCP result back into the value the tool returned.

    A tool returning a dict yields ONE TextContent; a tool returning a list
    yields one per element — so an empty list is zero items, not an error.
    Collapsing those was the first version of this helper and it made
    `list_machines() == []` look like a crash, which is precisely the empty
    state both clients have to render honestly."""
    import asyncio
    import json

    out = asyncio.run(mcp.call_tool(name, args))
    parts = out if isinstance(out, list) else [out]
    if not parts:
        return []
    decoded = [json.loads(p.text) for p in parts]
    return decoded[0] if len(decoded) == 1 else decoded


def _make_agent(url: str, agent_id: str, operator: bool = False) -> str:
    import httpx

    r = httpx.post(f"{url}/agents", json={"id": agent_id, "operator": operator},
                   headers={"Authorization": "Bearer k"}, timeout=5)
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


@pytest.fixture()
def hub(tmp_path):
    url, server, thread = _live_hub(tmp_path)
    yield url
    server.should_exit = True
    thread.join(timeout=10)


def test_the_spawn_tools_are_registered(hub, monkeypatch):
    import asyncio

    key = _make_agent(hub, "boss", operator=True)
    mcp = _server_against(hub, monkeypatch, key)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"spawn_seat", "list_spawns", "stop_spawn",
            "list_machines"} <= names


def test_spawn_seat_records_a_pending_row_through_the_real_route(hub, monkeypatch):
    key = _make_agent(hub, "boss", operator=True)
    mcp = _server_against(hub, monkeypatch, key)

    row = _call_tool(mcp, "spawn_seat",
                     {"seat_id": "scribe", "harness": "claude",
                      "mission": "write the minutes"})
    assert row["state"] == "pending"
    assert row["seat_id"] == "scribe" and row["machine"] == "local"
    assert row["requested_by"] == "boss"

    # One row -> one TextContent, so the helper hands back the dict itself.
    assert _call_tool(mcp, "list_spawns", {})["id"] == row["id"]


def test_spawn_seat_from_a_non_operator_fails_loudly(hub, monkeypatch):
    """The MCP lane must not soften a refusal into something an LLM reads as
    success — that is what the `ok: false` shape exists for."""
    key = _make_agent(hub, "member")
    mcp = _server_against(hub, monkeypatch, key)

    out = _call_tool(mcp, "spawn_seat",
                     {"seat_id": "scribe", "harness": "claude"})
    assert out["ok"] is False and out["error"] == 403
    assert out["detail"] == "this is an operator act"


def test_list_machines_is_readable_by_a_plain_member_and_is_empty(hub, monkeypatch):
    """A client cannot honestly say "no runner available" without being able
    to ask. An empty list is the answer, not a refusal."""
    key = _make_agent(hub, "member")
    mcp = _server_against(hub, monkeypatch, key)
    assert _call_tool(mcp, "list_machines", {}) == []


def test_stop_spawn_records_intent_only(hub, monkeypatch):
    import httpx

    key = _make_agent(hub, "boss", operator=True)
    runner_key = _make_agent(hub, "runner-mbp")
    admin = {"Authorization": "Bearer k"}
    httpx.put(f"{hub}/admin/machines/local/runner",
              json={"agent_id": "runner-mbp"}, headers=admin, timeout=5)

    mcp = _server_against(hub, monkeypatch, key)
    spawn_id = _call_tool(mcp, "spawn_seat",
                          {"seat_id": "scribe", "harness": "claude"})["id"]

    runner = {"Authorization": f"Bearer {runner_key}"}
    httpx.post(f"{hub}/spawns/claim", json={"machine": "local"},
               headers=runner, timeout=5)
    httpx.post(f"{hub}/spawns/{spawn_id}/state", json={"state": "running"},
               headers=runner, timeout=5)

    out = _call_tool(mcp, "stop_spawn", {"spawn_id": spawn_id})
    assert out["stop_requested_at"] is not None
    assert out["state"] == "running"      # only the runner ends a process
