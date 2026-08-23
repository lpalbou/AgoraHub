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


def _key_of(url: str, agent_id: str) -> str:
    """The api key handed back when that agent was registered in this test."""
    return _KEYS[agent_id]


_KEYS: dict[str, str] = {}


def _make_agent(url: str, agent_id: str, operator: bool = False) -> str:
    import httpx

    r = httpx.post(f"{url}/agents", json={"id": agent_id, "operator": operator},
                   headers={"Authorization": "Bearer k"}, timeout=5)
    assert r.status_code == 200, r.text
    _KEYS[agent_id] = r.json()["api_key"]
    return _KEYS[agent_id]


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


def test_spawn_seat_carries_the_knobs_through_the_mcp_door(hub, monkeypatch):
    """The MCP lane is the door laurent actually asked for — spawning from
    inside the chat rather than from a shell — so a knob that exists only on
    the HTTP payload does not exist for the operator who requested it.

    The refusal half is asserted here too, because the two doors must refuse
    at the SAME standard: a level the machine never announced is refused
    against the machine's own list, not an enum this hub holds.
    """
    import httpx

    key = _make_agent(hub, "boss", operator=True)
    runner_key = _make_agent(hub, "runner-mbp")
    httpx.put(f"{hub}/admin/machines/local/runner",
              json={"agent_id": "runner-mbp"},
              headers={"Authorization": "Bearer k"}, timeout=5)
    httpx.post(f"{hub}/machines/local/announce",
               json={"harnesses": ["claude"],
                     "capabilities": {"claude": {"reasoning": ["low", "high"]}}},
               headers={"Authorization": f"Bearer {runner_key}"}, timeout=5)

    mcp = _server_against(hub, monkeypatch, key)
    row = _call_tool(mcp, "spawn_seat",
                     {"seat_id": "scribe", "harness": "claude",
                      "model": "gpt-5.4", "reasoning": "high"})
    assert row["model"] == "gpt-5.4" and row["reasoning"] == "high"

    out = _call_tool(mcp, "spawn_seat",
                     {"seat_id": "scribe2", "harness": "claude",
                      "reasoning": "max"})
    assert out["ok"] is False and out["error"] == 400
    assert "low|high" in out["detail"]


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


# -- the OWED block must never name an exit the hub refuses -------------------

def _owed_text(mcp) -> str:
    """The rendered inbox, which is where the defect lived — the row DATA was
    already correct."""
    import asyncio
    out = asyncio.run(mcp.call_tool("check_inbox", {}))
    # FastMCP hands some tools back as (content, structured); take the
    # content side and keep only the pieces that carry rendered text.
    if isinstance(out, tuple):
        out = out[0]
    parts = out if isinstance(out, list) else [out]
    return "\n".join(getattr(p, "text", "") for p in parts)


def test_owed_line_for_another_seats_ask_names_the_exit_that_works(hub, monkeypatch):
    """agora-and-wui#244 + thread-shape-and-panels#32: two seats, one message,
    a lost turn each.

    A message whose `to` names bob, carrying an ask addressed to carol, gave
    bob `ANSWER … (pending ['1']) … answers=[...]` — and the hub then refused
    bob's `answers` AND `declines` ("you may not discharge ask ids not
    addressed to you"). Both named exits refused; the one that works — any
    plain reply — unmentioned.
    """
    import httpx

    admin = {"Authorization": "Bearer k"}
    alice = _make_agent(hub, "alice")
    bob_key = _make_agent(hub, "bob")
    _make_agent(hub, "carol")
    a = {"Authorization": f"Bearer {alice}"}
    b = {"Authorization": f"Bearer {bob_key}"}
    httpx.post(f"{hub}/channels", json={"name": "room", "private": False},
               headers=a, timeout=5)
    httpx.post(f"{hub}/channels/room/join", json={}, headers=b, timeout=5)
    httpx.post(f"{hub}/channels/room/join", json={},
               headers={"Authorization": f"Bearer {_key_of(hub,'carol')}"},
               timeout=5)

    r = httpx.post(f"{hub}/channels/room/messages",
                   json={"body": "park note", "title": "parked",
                         "status": "blocked", "to": ["bob", "carol"],
                         "asks": [{"id": "1", "text": "ping when green?",
                                   "to": ["carol"]}]},
                   headers=a, timeout=5)
    assert r.status_code == 200, r.text
    seq = r.json()["seq"]

    mcp = _server_against(hub, monkeypatch, bob_key)
    text = _owed_text(mcp)

    # The exit that works is named...
    assert f"REPLY room#{seq}" in text
    assert "ANY reply of yours clears this row" in text
    # ...the ids are marked as somebody else's...
    assert "ANOTHER seat's" in text
    # ...and neither refused exit is offered.
    assert f"ANSWER room#{seq}" not in text
    assert "answers=[...]" not in text.split(f"room#{seq}")[1].split("\n")[0]


def test_a_row_whose_asks_DO_name_you_still_says_ANSWER(hub, monkeypatch):
    """The other direction, and it is why the fix keys on the ask's own `to`
    rather than on whether asks exist. Delete the branch and this stays green;
    invert the condition and it goes red — so the pair pins the distinction,
    not just the new sentence."""
    import httpx

    alice = _make_agent(hub, "alice")
    bob_key = _make_agent(hub, "bob")
    a = {"Authorization": f"Bearer {alice}"}
    b = {"Authorization": f"Bearer {bob_key}"}
    httpx.post(f"{hub}/channels", json={"name": "room", "private": False},
               headers=a, timeout=5)
    httpx.post(f"{hub}/channels/room/join", json={}, headers=b, timeout=5)

    r = httpx.post(f"{hub}/channels/room/messages",
                   json={"body": "yours", "title": "q", "status": "open",
                         "asks": [{"id": "1", "text": "a?", "to": ["bob"]}]},
                   headers=a, timeout=5)
    seq = r.json()["seq"]

    text = _owed_text(_server_against(hub, monkeypatch, bob_key))
    assert f"ANSWER room#{seq}" in text
    assert "asks naming you: ['1']" in text
    assert f"REPLY room#{seq}" not in text


def test_each_ask_less_reason_gets_the_exit_the_hub_actually_honours(hub, monkeypatch):
    """The `reason` enum exists (dd07c45) because these cases look identical
    on the row and their exits INVERT. This renderer held the value and
    printed one generic sentence for all of them — the enum's own founding
    complaint, reproduced in the surface that reports it.

    Three reasons, three exits, and each assertion falsifies the other two:
    collapse any two branches and at least one goes red.
    """
    import httpx

    op_key = _make_agent(hub, "boss", operator=True)
    peer = _make_agent(hub, "alice")
    bob_key = _make_agent(hub, "bob")
    a = {"Authorization": f"Bearer {peer}"}
    b = {"Authorization": f"Bearer {bob_key}"}
    o = {"Authorization": f"Bearer {op_key}"}
    httpx.post(f"{hub}/channels", json={"name": "room", "private": False},
               headers=a, timeout=5)
    for h in (b, o):
        httpx.post(f"{hub}/channels/room/join", json={}, headers=h, timeout=5)

    # 1. an OPERATOR's ask-less request: only their word or resolved+evidence.
    httpx.post(f"{hub}/channels/room/messages",
               json={"body": "do the thing", "title": "op job",
                     "status": "blocked", "to": ["bob"]}, headers=o, timeout=5)
    # 2. a PEER's ask-less request: a claim row, not a reply.
    httpx.post(f"{hub}/channels/room/messages",
               json={"body": "peer job", "title": "peer job",
                     "status": "blocked", "to": ["bob"]}, headers=a, timeout=5)
    # 3. a directive debt: any reply clears it.
    root = httpx.post(f"{hub}/channels/room/messages",
                      json={"body": "root", "title": "root"},
                      headers=a, timeout=5).json()
    httpx.post(f"{hub}/channels/room/messages",
               json={"body": "pointing at you", "title": "yours",
                     "status": "reply", "reply_to": root["id"],
                     "to": ["bob"]}, headers=a, timeout=5)

    text = _owed_text(_server_against(hub, monkeypatch, bob_key))

    # The operator's: named REPORT, and it says a reply will not do.
    assert "REPORT room#" in text
    assert "data.evidence" in text
    # The peer's: named TAKE, and it names the claim row.
    assert "TAKE room#" in text
    assert "claim row citing this message" in text
    assert '"on it" is not delivery' in text
    # The directive debt: any reply.
    assert "REPLY room#" in text
    assert "ANY reply of yours clears this row" in text
    # And none of the three is offered the answers[] gesture, which discharges
    # nothing on any of them. Scoped to the OWED lines: the static triage
    # footer mentions `answers=[...]` as general guidance and is not a
    # per-row instruction, so asserting over the whole render would have
    # passed or failed for the wrong reason.
    owed_lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert owed_lines, "no OWED rows rendered"
    assert not any("answers=[...]" in ln for ln in owed_lines)
