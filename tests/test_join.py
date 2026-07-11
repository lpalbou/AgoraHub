"""Remote onboarding: join tokens, the AGORA1 artifact, invite/join CLI.

Three layers, tested at their real seams:

- hub (TestClient): the join-token lifecycle — mint/redeem/replay/id-lock/
  expiry/revocation/reuse, atomic consumption (a 409 id collision must NOT
  burn the token), operator=False forced, public-only channel auto-join, and
  POST /agents byte-identical to before (local onboarding untouched).
- artifact codec (pure): paste-safety — whitespace/line-wrap tolerance,
  truncation fails CLIENT-side with an actionable message, version gating.
- CLI flows (a real uvicorn hub on an ephemeral loopback port, tmp
  AGORA_HOME): `agora invite` -> paste line -> `agora join <blob>` lands the
  key in keys.json + config.json + the harness env block (the scrubbed-env
  channel), re-runs are repairs, and register/seed-key round-trip.

Nothing here touches the live hub, ~/.agora, or fixed ports.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from agora import config as _config
from agora.hub.app import create_app
from agora.join import decode_artifact, encode_artifact, parse_ttl, run_join

ADMIN_KEY = "test-admin-join"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    app = create_app(db_path=":memory:", admin_key=ADMIN_KEY, rate_per_minute=600.0)
    return TestClient(app)


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Empty AGORA_HOME + no ambient agora env, so tests can never read or
    write the operator's real ~/.agora."""
    home = tmp_path / "agora-home"
    monkeypatch.setenv("AGORA_HOME", str(home))
    for var in ("AGORA_URL", "AGORA_ADMIN_KEY", "AGORA_AGENT_ID", "AGORA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture()
def live_hub(tmp_path):
    """A real uvicorn hub on an EPHEMERAL loopback port (no fixed-port
    collisions), for flows that go through httpx rather than TestClient."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(db_path=str(tmp_path / "hub.db"), admin_key=ADMIN_KEY,
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
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", admin=ADMIN_KEY)
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "test hub did not shut down"


def _admin(key: str = ADMIN_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _mint(client: TestClient, **kwargs) -> dict:
    r = client.post("/join-tokens", json=kwargs, headers=_admin())
    assert r.status_code == 200, r.text
    return r.json()


def _join(client: TestClient, token: str, **kwargs):
    return client.post("/join", json={"token": token, **kwargs})


# ---------------------------------------------------------------------------
# hub: join-token lifecycle over HTTP
# ---------------------------------------------------------------------------


def test_mint_endpoints_are_admin_gated(client):
    for call in (
        lambda h: client.post("/join-tokens", json={"agent_id": "x"}, headers=h),
        lambda h: client.get("/join-tokens", headers=h),
        lambda h: client.delete("/join-tokens/abc", headers=h),
    ):
        assert call(_admin("not-the-admin")).status_code == 403
    # and the happy path still works with the real key
    assert client.get("/join-tokens", headers=_admin()).json() == []


def test_mint_validates_inputs(client):
    for bad in [{"ttl_seconds": 0}, {"ttl_seconds": -5},
                {"ttl_seconds": 31 * 86400.0},
                {"max_uses": 0}, {"max_uses": 101},
                {"agent_id": "Bad Id"}, {"agent_id": "hub"}]:
        r = client.post("/join-tokens", json={"agent_id": "ok", **bad},
                        headers=_admin())
        assert r.status_code == 400, (bad, r.text)
    # a token pinned to a TAKEN id could never be redeemed: refuse at mint
    client.post("/agents", json={"id": "castor"}, headers=_admin())
    r = client.post("/join-tokens", json={"agent_id": "castor"}, headers=_admin())
    assert r.status_code == 409


def test_redeem_registers_operator_false_and_joins_public_channels(client):
    owner = client.post("/agents", json={"id": "owner"}, headers=_admin()).json()
    owner_h = {"Authorization": f"Bearer {owner['api_key']}"}
    client.post("/channels", json={"name": "general", "private": False},
                headers=owner_h)
    client.post("/channels", json={"name": "vault", "private": True},
                headers=owner_h)

    minted = _mint(client, agent_id="castor", about="the entity",
                   channels=["general", "vault", "no-such-channel"])
    assert minted["token"].startswith("agora-join_")
    token_id, _, secret = minted["token"].removeprefix("agora-join_").partition(".")
    assert token_id == minted["token_id"] and len(secret) == 48

    r = _join(client, minted["token"])
    assert r.status_code == 200, r.text
    body = r.json()
    # operator=False is FORCED server-side (no field even exists on /join)
    assert body["agent"]["id"] == "castor"
    assert body["agent"]["operator"] is False
    assert body["agent"]["about"] == "the entity"
    # public channel joined; private + missing ones skipped, never fatal
    assert body["channels_joined"] == ["general"]
    key_h = {"Authorization": f"Bearer {body['api_key']}"}
    assert client.get("/whoami", headers=key_h).json()["id"] == "castor"
    channels = {c["name"]: c for c in client.get("/channels", headers=key_h).json()}
    assert channels["general"]["member"] is True
    assert "vault" not in channels or channels["vault"]["member"] is False

    # audit trail: uses counted, used_by recorded, no secrets anywhere
    [row] = client.get("/join-tokens", headers=_admin()).json()
    assert row["uses"] == 1 and row["used_by"] == ["castor"]
    assert "secret" not in json.dumps(row) and secret not in json.dumps(row)


def test_replay_of_a_spent_token_is_403_already_used(client):
    minted = _mint(client, agent_id="one-shot")
    assert _join(client, minted["token"]).status_code == 200
    replay = _join(client, minted["token"], agent_id="other")
    assert replay.status_code == 403
    assert replay.json()["detail"] == "join token already used"


def test_id_locked_token_rejects_other_ids(client):
    minted = _mint(client, agent_id="alpha")
    r = _join(client, minted["token"], agent_id="mallory")
    assert r.status_code == 403
    assert r.json()["detail"] == "join token is locked to 'alpha'"
    # the failed attempt consumed nothing: the pinned id still works
    assert _join(client, minted["token"]).status_code == 200


def test_expired_token_is_403_expired(client):
    minted = _mint(client, agent_id="late", ttl_seconds=0.05)
    time.sleep(0.12)
    r = _join(client, minted["token"])
    assert r.status_code == 403
    assert r.json()["detail"] == "join token expired"


def test_revoked_token_is_403_revoked(client):
    minted = _mint(client, agent_id="undone")
    r = client.delete(f"/join-tokens/{minted['token_id']}", headers=_admin())
    assert r.status_code == 200 and r.json()["revoked"] is True
    r = _join(client, minted["token"])
    assert r.status_code == 403
    assert r.json()["detail"] == "join token revoked"
    # revoking again is idempotent; revoking the unknown is a 404
    assert client.delete(f"/join-tokens/{minted['token_id']}",
                         headers=_admin()).status_code == 200
    assert client.delete("/join-tokens/deadbeef",
                         headers=_admin()).status_code == 404


def test_reusable_token_honors_max_uses(client):
    minted = _mint(client, max_uses=3)  # unpinned: fleet provisioning
    for name in ("fleet-a", "fleet-b", "fleet-c"):
        assert _join(client, minted["token"], agent_id=name).status_code == 200
    fourth = _join(client, minted["token"], agent_id="fleet-d")
    assert fourth.status_code == 403
    assert fourth.json()["detail"] == "join token already used"
    [row] = client.get("/join-tokens", headers=_admin()).json()
    assert row["uses"] == 3 and row["used_by"] == ["fleet-a", "fleet-b", "fleet-c"]


def test_409_id_collision_does_not_burn_the_token(client):
    client.post("/agents", json={"id": "taken"}, headers=_admin())
    minted = _mint(client, max_uses=1)
    r = _join(client, minted["token"], agent_id="taken")
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]
    # the token survived the collision: retry with a free id succeeds
    assert _join(client, minted["token"], agent_id="fresh").status_code == 200


def test_unpinned_token_requires_an_agent_id(client):
    minted = _mint(client, max_uses=1)
    r = _join(client, minted["token"])
    assert r.status_code == 400
    assert "pins no agent id" in r.json()["detail"]
    # the 400 consumed nothing
    assert _join(client, minted["token"], agent_id="chosen").status_code == 200


def test_wrong_secret_and_malformed_tokens_are_403_invalid(client):
    minted = _mint(client, agent_id="sec")
    token_id = minted["token_id"]
    for bad in (f"agora-join_{token_id}.{'0' * 48}",   # right id, wrong secret
                "agora-join_ffffffff.deadbeef",         # unknown id
                "agora-join_nodothere",                 # malformed shape
                "totally-not-a-token"):
        r = _join(client, bad, agent_id="sec")
        assert r.status_code == 403, bad
        assert r.json()["detail"] == "invalid join token"
    # a valid join token is NOT a bearer credential anywhere else
    assert client.get("/whoami", headers=_admin(minted["token"])).status_code == 401


def test_expired_tokens_are_lazily_purged_from_the_list(client):
    _mint(client, agent_id="gone", ttl_seconds=0.05)
    keeper = _mint(client, agent_id="stays")
    time.sleep(0.12)
    rows = client.get("/join-tokens", headers=_admin()).json()
    assert [r["token_id"] for r in rows] == [keeper["token_id"]]


def test_post_agents_is_untouched_by_the_join_feature(client):
    """Local onboarding must be byte-identical: same admin gate, same 403
    detail, and a join token must NOT work as a registration bearer."""
    r = client.post("/agents", json={"id": "eve"}, headers=_admin("nope"))
    assert r.status_code == 403
    assert r.json()["detail"] == "agent registration requires the admin key"

    minted = _mint(client, agent_id="sneaky")
    r = client.post("/agents", json={"id": "sneaky"},
                    headers=_admin(minted["token"]))
    assert r.status_code == 403  # the token is a /join credential, nothing more

    ok = client.post("/agents", json={"id": "local", "operator": True},
                     headers=_admin())
    assert ok.status_code == 200
    assert set(ok.json()) == {"agent", "api_key"}
    assert ok.json()["agent"]["operator"] is True


# ---------------------------------------------------------------------------
# artifact codec: paste-safety
# ---------------------------------------------------------------------------


def test_artifact_roundtrip_minimal_and_full():
    blob = encode_artifact("http://192.168.1.9:8765/", "agora-join_ab.cd")
    assert blob.startswith("AGORA1.") and "=" not in blob
    decoded = decode_artifact(blob)
    assert decoded == {"url": "http://192.168.1.9:8765",  # normalized ONCE
                       "token": "agora-join_ab.cd", "agent_id": None,
                       "channels": [], "expires_at": None}

    full = encode_artifact("http://h:1", "agora-join_ab.cd", agent_id="castor",
                           channels=["general", "ops"], expires_at=1783824187.9)
    decoded = decode_artifact(full)
    assert decoded["agent_id"] == "castor"
    assert decoded["channels"] == ["general", "ops"]
    assert decoded["expires_at"] == 1783824187  # display hint, int-truncated
    # secrets that must NEVER appear: nothing but url/token/id/channels/expiry
    assert set(json.loads(
        __import__("base64").urlsafe_b64decode(
            full.removeprefix("AGORA1.") + "=="))) <= {"u", "t", "a", "c", "e"}


def test_artifact_decode_survives_chat_linewraps_and_padding():
    blob = encode_artifact("http://h:1", "agora-join_ab.cd", agent_id="castor")
    wrapped = "\n  ".join([blob[:15], blob[15:40], blob[40:]]) + "\n"
    assert decode_artifact(wrapped)["agent_id"] == "castor"
    assert decode_artifact("\t" + blob + "  ")["token"] == "agora-join_ab.cd"


def test_artifact_truncation_fails_client_side_with_remedy():
    blob = encode_artifact("http://h:1", "agora-join_ab.cd", agent_id="castor")
    for cut in (blob[:-9], blob[: len("AGORA1.") + 4]):
        with pytest.raises(ValueError) as exc:
            decode_artifact(cut)
        assert "truncated paste" in str(exc.value)
        assert "agora invite" in str(exc.value)  # the ask-for-a-fresh-one remedy


def test_artifact_rejects_wrong_prefix_version_and_raw_tokens():
    with pytest.raises(ValueError, match="not an agora join artifact"):
        decode_artifact("hello world")
    with pytest.raises(ValueError, match="unsupported artifact version AGORA2"):
        decode_artifact("AGORA2.eyJ1IjoiaCJ9")
    # a raw token pasted where the artifact belongs gets redirected, not decoded
    with pytest.raises(ValueError, match="raw join token"):
        decode_artifact("agora-join_1a2b3c4d.9f9f9f")


def test_parse_ttl_units_and_errors():
    assert parse_ttl("90s") == 90.0
    assert parse_ttl("30m") == 1800.0
    assert parse_ttl("24h") == 86400.0
    assert parse_ttl("7d") == 7 * 86400.0
    for bad in ("", "24", "h", "-2h", "0d", "2w", "soon"):
        with pytest.raises(ValueError, match="invalid ttl"):
            parse_ttl(bad)


# ---------------------------------------------------------------------------
# CLI flows against a real hub (ephemeral port, tmp home)
# ---------------------------------------------------------------------------


def _parse_cli(argv: list[str]):
    from agora.cli import build_parser
    return build_parser().parse_args(argv)


def _run_cli(argv: list[str]) -> None:
    args = _parse_cli(argv)
    args.func(args)


def _extract_paste_line(banner: str) -> str:
    match = re.search(r"agora join (AGORA1\.\S+)", banner)
    assert match, f"no paste line in banner:\n{banner}"
    return match.group(1)


def test_invite_then_join_end_to_end(live_hub, isolated_home, tmp_path, capsys):
    """The whole approved end-state, through the real CLI entry points:
    operator mints ONE paste line; the remote runs it; every credential sink
    a surface reads is populated; the admin key never lands on disk."""
    seed = httpx.post(f"{live_hub.url}/agents", json={"id": "seed"},
                      headers=_admin(), timeout=5).json()
    httpx.post(f"{live_hub.url}/channels",
               json={"name": "general", "private": False},
               headers={"Authorization": f"Bearer {seed['api_key']}"}, timeout=5)

    _run_cli(["invite", "castor", "--channels", "general", "--url",
              live_hub.url, "--admin-key", ADMIN_KEY, "--about", "the entity"])
    banner = capsys.readouterr().out
    assert "single-use" in banner and "revoke" in banner
    blob = _extract_paste_line(banner)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _run_cli(["join", blob, "--workspace", str(workspace), "--with-hook"])
    out = capsys.readouterr().out

    # keys.json: the ONE url-qualified entry every CLI surface reads, 0600
    keys_path = isolated_home / "keys.json"
    keys = json.loads(keys_path.read_text())
    api_key = keys[f"{live_hub.url}::castor"]
    assert api_key.startswith("agora_")
    assert keys_path.stat().st_mode & 0o077 == 0

    # config.json: url only — NEVER an admin key on a remote — and 0600
    cfg = json.loads((isolated_home / "config.json").read_text())
    assert cfg == {"url": live_hub.url}
    assert (isolated_home / "config.json").stat().st_mode & 0o077 == 0

    # workspace wiring: env block carries the key (the scrubbed-env channel)
    mcp_path = workspace / ".cursor" / "mcp.json"
    env = json.loads(mcp_path.read_text())["mcpServers"]["agora"]["env"]
    assert env["AGORA_URL"] == live_hub.url          # the SAME normalized string
    assert env["AGORA_AGENT_ID"] == "castor"
    assert env["AGORA_API_KEY"] == api_key
    assert mcp_path.stat().st_mode & 0o077 == 0
    assert (workspace / ".cursor" / "rules" / "agora.md").exists()
    assert (workspace / ".cursor" / "hooks.json").exists()

    assert "verified    -> GET /whoami as 'castor' OK (channels: general)" in out
    assert "gitignore" in out
    assert "Do not run `agora up` on this machine" in out

    # the hub agrees end to end: key authenticates, membership landed
    who = httpx.get(f"{live_hub.url}/whoami",
                    headers={"Authorization": f"Bearer {api_key}"}, timeout=5)
    assert who.json()["id"] == "castor" and who.json()["operator"] is False

    # re-running the SAME (now spent) blob is a repair, not an error
    _run_cli(["join", blob, "--workspace", str(workspace), "--with-hook"])
    rerun = capsys.readouterr().out
    assert "skipping redemption" in rerun
    assert json.loads(keys_path.read_text())[f"{live_hub.url}::castor"] == api_key

    # ...and the channel-join mode of the SAME subparser still works
    _run_cli(["join", "--channel", "general", "--as", "castor"])
    joined = json.loads(capsys.readouterr().out)
    assert joined["joined"] is True and joined["channel"]["name"] == "general"


def test_invite_warns_on_loopback_url(live_hub, isolated_home, capsys):
    _run_cli(["invite", "remote-a", "--url", live_hub.url,
              "--admin-key", ADMIN_KEY])
    banner = capsys.readouterr().out
    assert "WARNING" in banner and "loopback" in banner and "--url" in banner


def test_invite_list_and_revoke_via_cli(live_hub, isolated_home, capsys):
    _run_cli(["invite", "listed", "--url", live_hub.url,
              "--admin-key", ADMIN_KEY])
    token_id = re.search(r"token id: (\w+)", capsys.readouterr().out).group(1)

    _run_cli(["invite", "--list", "--url", live_hub.url,
              "--admin-key", ADMIN_KEY])
    listing = capsys.readouterr().out
    assert token_id in listing and "listed" in listing and "live" in listing
    assert "agora-join_" not in listing  # never a secret on the audit surface

    _run_cli(["invite", "--revoke", token_id, "--url", live_hub.url,
              "--admin-key", ADMIN_KEY])
    assert "revoked" in capsys.readouterr().out
    _run_cli(["invite", "--list", "--url", live_hub.url,
              "--admin-key", ADMIN_KEY])
    assert "revoked" in capsys.readouterr().out


def test_invite_usage_errors_and_missing_admin_key(isolated_home):
    with pytest.raises(SystemExit, match="OR --any-id"):
        _run_cli(["invite", "x", "--any-id", "--admin-key", "k"])
    with pytest.raises(SystemExit, match="name the agent"):
        _run_cli(["invite", "--admin-key", "k"])
    with pytest.raises(SystemExit, match="invalid ttl"):
        _run_cli(["invite", "x", "--ttl", "soon", "--admin-key", "k"])
    with pytest.raises(SystemExit, match="no admin key"):
        _run_cli(["invite", "x"])


def test_join_subparser_disambiguation(isolated_home):
    """One subparser, two verbs: both = loud error, neither = loud error,
    channel mode still demands --as, --token demands --url. All client-side:
    no hub exists at any url these could reach."""
    blob = encode_artifact("http://127.0.0.1:1", "agora-join_aa.bb", agent_id="x")
    with pytest.raises(SystemExit, match="choose ONE mode"):
        _run_cli(["join", blob, "--channel", "general"])
    with pytest.raises(SystemExit, match="nothing to do"):
        _run_cli(["join"])
    with pytest.raises(SystemExit, match="requires --as"):
        _run_cli(["join", "--channel", "general"])
    with pytest.raises(SystemExit, match="needs --url"):
        _run_cli(["join", "--token", "agora-join_aa.bb"])
    with pytest.raises(SystemExit, match="truncated paste"):
        _run_cli(["join", blob[:-10]])


def test_join_id_choice_errors_are_client_side(isolated_home):
    """Pin conflicts and missing ids fail BEFORE any network call — the urls
    here point at a closed port, so reaching for the hub would error
    differently ('cannot reach')."""
    pinned = encode_artifact("http://127.0.0.1:1", "agora-join_aa.bb",
                             agent_id="castor")
    with pytest.raises(SystemExit, match="locked to 'castor'"):
        _run_cli(["join", pinned, "--as", "other"])
    unpinned = encode_artifact("http://127.0.0.1:1", "agora-join_aa.bb")
    with pytest.raises(SystemExit, match="pins no agent id"):
        _run_cli(["join", unpinned])


def test_join_surfaces_hub_refusals_with_remedies(live_hub, isolated_home,
                                                  tmp_path, capsys):
    """Distinct hub 403/409 details reach the human with the next step
    attached; a 409 tells them the token SURVIVED."""
    minted = httpx.post(f"{live_hub.url}/join-tokens", json={"max_uses": 2},
                        headers=_admin(), timeout=5).json()
    httpx.post(f"{live_hub.url}/agents", json={"id": "taken"},
               headers=_admin(), timeout=5)
    blob = encode_artifact(live_hub.url, minted["token"])
    with pytest.raises(SystemExit, match="NOT consumed"):
        _run_cli(["join", blob, "--as", "taken", "--harness", "none"])
    # the same artifact still redeems for a free id afterwards
    _run_cli(["join", blob, "--as", "fresh", "--harness", "none"])
    assert "verified    -> GET /whoami as 'fresh' OK" in capsys.readouterr().out

    revoked = httpx.post(f"{live_hub.url}/join-tokens",
                         json={"agent_id": "nope"}, headers=_admin(),
                         timeout=5).json()
    httpx.delete(f"{live_hub.url}/join-tokens/{revoked['token_id']}",
                 headers=_admin(), timeout=5)
    with pytest.raises(SystemExit, match="join token revoked"):
        _run_cli(["join", encode_artifact(live_hub.url, revoked["token"],
                                          agent_id="nope")])

    with pytest.raises(SystemExit, match="cannot reach the hub"):
        _run_cli(["join", encode_artifact("http://127.0.0.1:9", "agora-join_a.b",
                                          agent_id="x")])


def test_register_and_seed_key_roundtrip(live_hub, isolated_home, capsys):
    """Path B: the operator mints ONE per-agent key (`agora register`, key
    shown once, never cached locally) and the agent machine imports it
    (`agora seed-key`, verified against the hub at paste time)."""
    _run_cli(["register", "pollux", "--url", live_hub.url,
              "--admin-key", ADMIN_KEY, "--about", "remote laptop"])
    out = capsys.readouterr().out
    key = re.search(r"api_key: (agora_\w+)", out).group(1)
    assert "exactly ONCE" in out and "seed-key" in out
    # register does NOT cache: the key belongs to another machine
    assert _config.get_cached_key(live_hub.url, "pollux") is None

    with pytest.raises(SystemExit, match="already registered"):
        _run_cli(["register", "pollux", "--url", live_hub.url,
                  "--admin-key", ADMIN_KEY])

    _run_cli(["seed-key", "pollux", "--url", live_hub.url, "--key", key])
    out = capsys.readouterr().out
    assert "verified: GET /whoami as 'pollux' OK" in out
    assert _config.get_cached_key(live_hub.url, "pollux") == key
    assert (isolated_home / "keys.json").stat().st_mode & 0o077 == 0

    with pytest.raises(SystemExit, match="rejected this key"):
        _run_cli(["seed-key", "other", "--url", live_hub.url,
                  "--key", "agora_truncated"])


def test_setup_cursor_with_key_seeds_caches_and_embeds(live_hub, isolated_home,
                                                       tmp_path, capsys):
    """`setup-* --key` is the no-new-infrastructure remote path: seed the
    operator-minted key, verify it, and land it in BOTH sinks (keys.json for
    CLI/hook, the env block for the scrubbed MCP server)."""
    minted = httpx.post(f"{live_hub.url}/agents", json={"id": "helios"},
                        headers=_admin(), timeout=5).json()
    workspace = tmp_path / "ws2"
    workspace.mkdir()
    _run_cli(["setup-cursor", "helios", "--workspace", str(workspace),
              "--url", live_hub.url, "--key", minted["api_key"]])
    out = capsys.readouterr().out
    assert "authenticates immediately" in out and "gitignore" in out

    assert _config.get_cached_key(live_hub.url, "helios") == minted["api_key"]
    mcp_path = workspace / ".cursor" / "mcp.json"
    env = json.loads(mcp_path.read_text())["mcpServers"]["agora"]["env"]
    assert env["AGORA_API_KEY"] == minted["api_key"]
    assert env["AGORA_URL"] == live_hub.url
    assert mcp_path.stat().st_mode & 0o077 == 0

    with pytest.raises(SystemExit, match="rejected this key"):
        _run_cli(["setup-cursor", "helios", "--workspace", str(workspace),
                  "--url", live_hub.url, "--key", "agora_wrong"])


def test_scrubbed_env_mcp_resolution_after_join(live_hub, isolated_home,
                                                tmp_path, monkeypatch, capsys):
    """Simulate cursor-agent's env scrub: ONLY the mcp.json env block plus an
    EMPTY home survive. The MCP server must still resolve credentials —
    that is what embedding AGORA_API_KEY buys."""
    minted = httpx.post(f"{live_hub.url}/join-tokens",
                        json={"agent_id": "scrub"}, headers=_admin(),
                        timeout=5).json()
    workspace = tmp_path / "ws3"
    workspace.mkdir()
    run_join(url=live_hub.url, token=minted["token"], agent_id=None, about="",
             harness="cursor", workspace=str(workspace), with_hook=False,
             listen=False, mcp_command="agora-mcp")
    env = json.loads((workspace / ".cursor" / "mcp.json").read_text()
                     )["mcpServers"]["agora"]["env"]

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.delenv("AGORA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(empty_home))       # HOME survives the scrub
    for var, value in env.items():                    # the env block survives
        monkeypatch.setenv(var, value)

    from agora.mcp.server import _resolve_credentials
    base_url, api_key = _resolve_credentials()
    assert base_url == live_hub.url
    r = httpx.get(f"{base_url}/whoami",
                  headers={"Authorization": f"Bearer {api_key}"}, timeout=5)
    assert r.status_code == 200 and r.json()["id"] == "scrub"


def test_mcp_no_key_error_is_surface_aware(isolated_home, monkeypatch):
    """The misleading-error fix: with no credential anywhere, a REMOTE url
    must point at the join flow (never 'run agora up', which would start a
    wrong local hub); a LOOPBACK url keeps today's advice."""
    from agora.mcp.server import _resolve_credentials

    monkeypatch.setenv("AGORA_AGENT_ID", "ghost")
    monkeypatch.setenv("AGORA_URL", "http://192.168.1.50:8765")
    with pytest.raises(SystemExit) as exc:
        _resolve_credentials()
    remote_msg = str(exc.value)
    assert "agora join" in remote_msg
    assert "another machine" in remote_msg
    assert remote_msg.count("agora up") <= 1     # only as the warned-AGAINST step
    assert "Run `agora up`" not in remote_msg

    monkeypatch.setenv("AGORA_URL", "http://127.0.0.1:8765")
    with pytest.raises(SystemExit) as exc:
        _resolve_credentials()
    assert "Run `agora up`" in str(exc.value)
