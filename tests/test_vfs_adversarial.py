"""Adversarial audit of the uncommitted vfs-binary + mentions work.

Every test here encodes a FINDING; all are green ("attacked, held") and PIN the
current behavior. The mentions logic settled on **seat-identity precedence**
(operator ruling): a token matching a registered seat id is a mention even when
written `@seat:` / `@seat/...`; only tokens matching NO registered seat and
followed by `/`/`:` read as vfs references. These tests pin that ruling and the
one residual trade-off it creates (folder/channel names that collide with a
registered seat id).

Run with the project venv (a stale installed wheel would shadow src/):
    .venv/bin/python -m pytest tests/test_vfs_adversarial.py -q
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agora.chat import parse_group
from agora.hub.app import create_app
from agora.hub.service import HubError, HubService, _b64_decoded_size
from agora.db import Database, StoreConflict
from agora.mentions import parse_mentions, resolve_mentions

try:
    from agora.models import MAX_FS_BINARY_BYTES
except ImportError:  # stale installed agora shadowing src/
    pytest.skip("imported 'agora' predates fs binary support (run via .venv)",
                allow_module_level=True)

ADMIN_KEY = "test-admin"
PAYLOAD = bytes(range(256)) * 4          # not valid UTF-8
PAYLOAD_B64 = base64.b64encode(PAYLOAD).decode()


# ===========================================================================
# harness
# ===========================================================================

def make_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=":memory:", admin_key=ADMIN_KEY,
                                 rate_per_minute=600.0,
                                 notify_dir=str(tmp_path / "notify")))


def register(client: TestClient, agent_id: str, operator: bool = False) -> dict:
    r = client.post("/agents", json={"id": agent_id, "operator": operator},
                    headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_channel(client: TestClient, owner: dict, name: str, *members: dict) -> None:
    client.post("/channels", json={"name": name}, headers=owner)
    for m in members:
        tok = client.post(f"/channels/{name}/invites", json={},
                          headers=owner).json()["invite_token"]
        client.post(f"/channels/{name}/join", json={"invite_token": tok}, headers=m)


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


@pytest.fixture()
def agents(service):
    alice, _ = service.register_agent("alice", "Alice")
    bob, _ = service.register_agent("bob", "Bob")
    service.create_channel(alice, "design", private=True)
    tok = service.create_invite(alice, "design", invitee="bob")
    service.join_channel(bob, "design", invite_token=tok)
    return alice, bob


def _authored(rows: list) -> list:
    return [r for r in rows if not r["path"].startswith("channel/")]


# ===========================================================================
# 1. MENTIONS — lost / false obligations
# ===========================================================================

@pytest.mark.parametrize("body,expect", [
    ("ship it @seat, thanks", ["seat"]),      # comma
    ("done @seat", ["seat"]),                 # end-of-string
    ("(@seat)", ["seat"]),                    # close paren
    ("@seat\n next", ["seat"]),               # newline
    ("@seat\r\n next", ["seat"]),             # CRLF
    ("@seat; go", ["seat"]),                  # semicolon
    ("@seat! now", ["seat"]),                 # bang
    ("@seat? ok", ["seat"]),                  # question mark
])
def test_punctuation_and_whitespace_after_seat_still_mention(body, expect):
    # Only '/' and ':' demote a token to a vfs ref; all other trailing
    # punctuation/whitespace must leave the mention intact.
    assert parse_mentions(body) == expect


def test_trailing_period_is_swallowed_into_token_preexisting():
    # '.' is legal inside the mention class, so a sentence-final dot is
    # consumed (pre-existing regex behavior, NOT introduced here). "seat."
    # is not a valid agent id, so it resolves to nobody — no false obligation.
    assert parse_mentions("ship it @seat. next") == ["seat."]


def test_registry_free_parse_mentions_drops_all_pathlike(agents):
    # parse_mentions is the REGISTRY-FREE safe subset (has no seat list, so it
    # cannot apply seat precedence): every path-like token is dropped. It is
    # NOT used to mint obligations — the hub uses resolve_mentions (below).
    assert parse_mentions("roadmap in @plans/q3.md now") == []
    assert parse_mentions("logo @design:assets/logo.png") == []
    assert parse_mentions("@a//b") == []
    assert parse_mentions("@a:/b") == []
    assert parse_mentions("@alice: please review") == []   # vocative, no registry here


def test_mention_adjacent_to_ref_still_counts():
    assert parse_mentions("@laurent see @plans/q3.md") == ["laurent"]


# ---- seat-identity precedence (the settled ruling): the registry decides ----

def test_resolve_seat_precedence_semantics():
    members = {"alice", "core"}
    registered = {"alice", "core", "plans"}
    # vocative colon on a member -> mention (the vocative-colon lost-mention is
    # FIXED here; parse_mentions alone would have dropped it).
    assert resolve_mentions("@alice: review", members, registered) == (["alice"], [])
    # path-like token that IS a registered member -> seat wins (mention).
    assert resolve_mentions("@alice/notes.md", members, registered) == (["alice"], [])
    # path-like token that is a registered NON-member -> outsider warning
    # (fires from the wrong room, exactly as a plain @nonmember would).
    assert resolve_mentions("@plans/q3.md", members, registered) == ([], ["plans"])
    # path-like token matching NO registered seat -> genuine vfs ref: silent.
    assert resolve_mentions("@ghostdir/q3.md", members, registered) == ([], [])
    # one PLAIN occurrence anywhere clears the path-like flag.
    assert resolve_mentions("@core:src/x and @core", members, registered) == (["core"], [])


def test_vocative_colon_obliges_registered_member_end_to_end(tmp_path):
    # End-to-end proof the vocative-colon form addresses a member (no lost
    # obligation). "attacked, held".
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    alice = register(client, "alice")
    make_channel(client, op, "room", alice)
    for body in ("@alice please review", "@alice: please review"):
        r = client.post("/channels/room/messages", json={
            "body": body, "title": "d", "status": "open"}, headers=op)
        assert r.status_code == 200, r.text
        assert "alice" in (r.json()["to"] or []), body
    # ask-text path too: the vocative-colon ask obliges alice (shows up in her
    # /owed.to_answer — the post response does not echo per-ask `to`).
    r = client.post("/channels/room/messages", json={
        "body": "kickoff", "title": "d", "status": "open",
        "asks": [{"id": "a1", "text": "@alice: sign off"}]}, headers=op)
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    owed = client.get("/owed", headers=alice).json()
    assert any(row.get("id") == mid for row in owed.get("to_answer", []))


def test_pathlike_ref_to_non_seat_folder_mints_nothing_end_to_end(tmp_path):
    # A vfs reference whose folder name is NOT a registered seat obliges nobody
    # and raises no outsider warning.
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    alice = register(client, "alice")
    make_channel(client, op, "room", alice)
    r = client.post("/channels/room/messages", json={
        "body": "roadmap is in @plans/q3.md — read it", "title": "d",
        "status": "open"}, headers=op)
    assert r.status_code == 200, r.text
    assert "plans" not in (r.json()["to"] or [])
    owed = client.get("/owed", headers=alice).json()
    assert not any(row.get("id") == r.json()["id"]
                   for row in owed.get("to_answer", []))


def test_finding_p2_namespace_collision_folder_named_like_seat(tmp_path):
    # RESIDUAL TRADE-OFF (documented in protocol.md): if a vfs folder / channel
    # name collides with a registered seat id, a path reference to it mints an
    # obligation on that seat. Here seat 'plans' is a MEMBER and the author
    # writes '@plans/q3.md' meaning a FILE under folder 'plans' — but 'plans'
    # gets obliged. Operator-ruled intentional (seat identity wins), so this is
    # informational, not a defect. Severity P2.
    client = make_client(tmp_path)
    op = register(client, "op", operator=True)
    plans = register(client, "plans")           # a seat that shares the folder name
    make_channel(client, op, "room", plans)
    r = client.post("/channels/room/messages", json={
        "body": "see @plans/q3.md", "title": "d", "status": "open"}, headers=op)
    assert r.status_code == 200, r.text
    assert "plans" in (r.json()["to"] or [])     # the seat was obliged, not the file


# ---- Unicode lookalike separators: token still mentions (verdict) ----

@pytest.mark.parametrize("sep,name", [
    ("\u2215", "U+2215 division slash"),
    ("\uff0f", "U+FF0F fullwidth solidus"),
    ("\u2044", "U+2044 fraction slash"),
    ("\uff1a", "U+FF1A fullwidth colon"),
])
def test_unicode_lookalike_separators_still_mention(sep, name):
    # Lookalikes are not ASCII '/'/':' so path_like is False -> a plain
    # candidate. VERDICT: not an escalation — the obligation lands on the
    # literally-named seat, which a plain "@seat" already does. Only novelty is
    # DISGUISE (body reads path-like to a human), and the obligation is still
    # fully visible in `to`/the ledger. Low severity.
    assert parse_mentions(f"@victim{sep}file.md") == ["victim"]
    assert resolve_mentions(f"@victim{sep}file.md", {"victim"}, {"victim"}) == (
        ["victim"], [])


# ---- parse_group is a DIFFERENT surface and is deliberately unchanged ----

def test_parse_group_unaffected_by_mentions_change():
    # /group tokenizes @mentions into a roster; it has no '/:' skip and must
    # not grow one. A colon/slash still just ends the token.
    title, members = parse_group("Fix @gateway:x.md then @core/y with @entity")
    assert members == ["gateway", "core", "entity"]


# ---- quote-fence exclusion still holds around refs ----

def test_quote_fence_exclusion_holds_around_refs():
    body = ("\u27e6AGORA:xy:quote\u27e7@ghost read @plans/q3.md"
            "\u27e6/AGORA:xy\u27e7 @agora check @plans/q3.md")
    assert parse_mentions(body) == ["agora"]


# ===========================================================================
# 2. exactly-one-of vs EMPTY files (is-None, not truthiness)
# ===========================================================================

def test_empty_text_file_writes(service, agents):
    alice, _ = agents
    w = service.fs_write(alice, "design", "empty.md", "")
    assert w.version == 1 and w.size_bytes == 0
    assert w.content == "" and w.encoding is None and w.content_b64 is None
    assert service.fs_read(alice, "design", "empty.md").content == ""


def test_empty_binary_file_writes(service, agents):
    alice, _ = agents
    w = service.fs_write(alice, "design", "empty.bin", content_b64="")
    assert w.version == 1 and w.size_bytes == 0
    assert w.encoding == "base64" and w.content_b64 == "" and w.content == ""
    r = service.fs_read(alice, "design", "empty.bin")
    assert r.encoding == "base64" and r.size_bytes == 0


def test_both_and_neither_rejected(service, agents):
    alice, _ = agents
    with pytest.raises(HubError) as e1:
        service.fs_write(alice, "design", "x", "t", content_b64=PAYLOAD_B64)
    assert e1.value.status_code == 400
    with pytest.raises(HubError) as e2:
        service.fs_write(alice, "design", "x")
    assert e2.value.status_code == 400


def test_http_empty_text_and_empty_binary(tmp_path):
    client = make_client(tmp_path)
    alice = register(client, "alice")
    client.post("/channels", json={"name": "design"}, headers=alice)
    t = client.put("/channels/design/fs/e.md", json={"content": ""}, headers=alice)
    assert t.status_code == 200 and t.json()["size_bytes"] == 0
    b = client.put("/channels/design/fs/e.bin", json={"content_b64": ""}, headers=alice)
    assert b.status_code == 200
    assert b.json()["encoding"] == "base64" and b.json()["size_bytes"] == 0


# ===========================================================================
# 3. BINARY WRITE HARDENING
# ===========================================================================

@pytest.mark.parametrize("bad", [
    "YQ==\n",          # trailing newline
    "YQ ==",           # internal space
    " YQ==",           # leading space
    "abc",             # length not multiple of 4
    "AAA=AAA=",        # padding in the middle
    "not base64!!",    # alphabet violation
])
def test_strict_base64_refuses_laundering(service, agents, bad):
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "x.bin", content_b64=bad)
    assert e.value.status_code == 400


def test_urlsafe_alphabet_is_refused(service, agents):
    # bytes that force '+' and '/' in STANDARD base64; the url-safe form uses
    # '-'/'_' which are outside the standard alphabet -> validate=True rejects.
    raw = b"\xfb\xff\xbf" * 3
    std = base64.b64encode(raw).decode()
    url = base64.urlsafe_b64encode(raw).decode()
    assert std != url and ("-" in url or "_" in url)
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "u.bin", content_b64=url)
    assert e.value.status_code == 400
    # ...but the standard-alphabet encoding of the same bytes is accepted.
    assert service.fs_write(alice, "design", "u.bin",
                            content_b64=std).size_bytes == len(raw)


def test_missing_padding_refused(service, agents):
    alice, _ = agents
    unpadded = base64.b64encode(b"a").decode().rstrip("=")   # "YQ" (needs "==")
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "p.bin", content_b64=unpadded)
    assert e.value.status_code == 400


def test_content_b64_with_markdown_mime_is_flipped_to_octet_stream(service, agents):
    alice, _ = agents
    # default mime omitted -> arrives as text/markdown -> flipped
    w = service.fs_write(alice, "design", "a.bin", content_b64=PAYLOAD_B64)
    assert w.mime == "application/octet-stream"
    # EXPLICIT text/markdown is indistinguishable from the default -> also flipped
    w2 = service.fs_write(alice, "design", "b.bin", content_b64=PAYLOAD_B64,
                          mime="text/markdown")
    assert w2.mime == "application/octet-stream"
    # A non-markdown text mime is KEPT verbatim; but the read still yields
    # content=="" so nothing downstream can render the base64 as text.
    w3 = service.fs_write(alice, "design", "c.bin", content_b64=PAYLOAD_B64,
                          mime="text/html")
    assert w3.mime == "text/html"
    r = service.fs_read(alice, "design", "c.bin")
    assert r.content == "" and r.encoding == "base64"


def test_text_binary_text_overwrite_all_three_versions(service, agents):
    alice, bob = agents
    service.fs_write(alice, "design", "doc", "# text v1")
    service.fs_write(bob, "design", "doc", content_b64=PAYLOAD_B64, mime="image/png")
    service.fs_write(alice, "design", "doc", "text v3")
    v1 = service.fs_read(bob, "design", "doc", version=1)
    v2 = service.fs_read(bob, "design", "doc", version=2)
    v3 = service.fs_read(bob, "design", "doc")
    assert v1.content == "# text v1" and v1.encoding is None
    assert v2.encoding == "base64" and base64.b64decode(v2.content_b64) == PAYLOAD
    assert v2.mime == "image/png" and v2.size_bytes == len(PAYLOAD)
    assert v3.content == "text v3" and v3.encoding is None and v3.version == 3


def test_cas_conflict_on_binary(service, agents):
    alice, bob = agents
    service.fs_write(alice, "design", "c.bin", content_b64=PAYLOAD_B64, expect_version=0)
    with pytest.raises(StoreConflict):
        service.fs_write(bob, "design", "c.bin", content_b64=PAYLOAD_B64, expect_version=0)


def test_channel_prefix_authority_for_binary(service, agents):
    _, bob = agents      # plain member
    with pytest.raises(HubError) as e:
        service.fs_write(bob, "design", "channel/logo.png", content_b64=PAYLOAD_B64)
    assert e.value.status_code == 403


def test_charter_binary_refused_even_for_owner(service, agents):
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "channel/charter.md", content_b64=PAYLOAD_B64)
    assert e.value.status_code == 400 and "charter must be text" in e.value.detail


def test_charter_binary_sneak_via_delete_then_recreate_refused(service, agents):
    # Seed a text charter, delete it, then try to re-create it as binary.
    alice, _ = agents
    service.fs_write(alice, "design", "channel/charter.md", "# Charter")
    service.fs_delete(alice, "design", "channel/charter.md")
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "channel/charter.md",
                         content_b64=PAYLOAD_B64, expect_version=0)
    assert e.value.status_code == 400 and "charter must be text" in e.value.detail


def test_charter_binary_sneak_via_expect_version_refused(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "channel/charter.md", "# Charter")   # v1
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "channel/charter.md",
                         content_b64=PAYLOAD_B64, expect_version=1)
    assert e.value.status_code == 400 and "charter must be text" in e.value.detail


def test_decoded_size_cap_boundary(service, agents):
    alice, _ = agents
    over = base64.b64encode(b"\x00" * (MAX_FS_BINARY_BYTES + 1)).decode()
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "big.bin", content_b64=over)
    assert e.value.status_code == 413
    at = base64.b64encode(b"\x00" * MAX_FS_BINARY_BYTES).decode()
    assert service.fs_write(alice, "design", "big.bin",
                            content_b64=at).size_bytes == MAX_FS_BINARY_BYTES


# ===========================================================================
# 4. fs_list INTEGRITY (compensation head-fetch)
# ===========================================================================

def test_list_mixed_text_binary_empty_and_deleted(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "notes.md", "# Notes\nbody")   # text, non-empty
    service.fs_write(alice, "design", "img.png", content_b64=PAYLOAD_B64,
                     description="the logo")                          # binary
    service.fs_write(alice, "design", "empty.md", "")                # empty text, size 0
    service.fs_write(alice, "design", "empty.bin", content_b64="")   # empty binary, size 0
    service.fs_write(alice, "design", "gone.md", "temp")
    service.fs_delete(alice, "design", "gone.md")                    # tombstoned
    rows = {r["path"]: r for r in _authored(service.fs_list(alice, "design"))}

    assert "gone.md" not in rows                                     # tombstone hidden
    assert rows["notes.md"]["size"] == len("# Notes\nbody")
    assert "encoding" not in rows["notes.md"]
    assert rows["img.png"]["encoding"] == "base64"
    assert rows["img.png"]["size"] == len(PAYLOAD)                   # DECODED bytes
    # BOTH-empty guard: empty text stays text (no marker); empty binary is
    # marked base64 — the head-fetch compensation distinguishes them.
    assert "encoding" not in rows["empty.md"] and rows["empty.md"]["size"] == 0
    assert rows["empty.bin"]["encoding"] == "base64" and rows["empty.bin"]["size"] == 0


def test_delete_then_recreate_as_binary_lists_correctly(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "p", "was text")   # v1 text
    service.fs_delete(alice, "design", "p")              # v2 tombstone
    w = service.fs_write(alice, "design", "p", content_b64=PAYLOAD_B64)  # v3 binary
    assert w.version == 3                                # monotonic across delete
    [row] = [r for r in _authored(service.fs_list(alice, "design")) if r["path"] == "p"]
    assert row["encoding"] == "base64" and row["size"] == len(PAYLOAD)
    assert row["version"] == 3


# ===========================================================================
# 5. _b64_decoded_size never disagrees with a real decode (canonical b64)
# ===========================================================================

def test_b64_decoded_size_matches_real_decode():
    import os
    for n in list(range(0, 200)) + [1000, 4096]:
        b64 = base64.b64encode(os.urandom(n)).decode()
        assert _b64_decoded_size(b64) == len(base64.b64decode(b64, validate=True)) == n
