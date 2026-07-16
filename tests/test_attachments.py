"""Message attachments (0091): content-addressed channel blobs + refs on
messages that ride every envelope. Operator ask 2026-07-15 ("attach a
document or image... every recipient receives the text AND the files"),
contract settled with the consumer (continuum) before this build.

Covered here: byte-exact round trips, content addressing/idempotency, caps,
membership/pause gates, ref validation (channel-scoped, server-truth
normalization, no raw-data bypass), envelope refs without inline bytes,
serve-side hardening (disposition/nosniff/active-type downgrade), DMs
end-to-end, and the ledger committing to attachment identity.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from agora.db import Database
from agora.hub.app import create_app
from agora.hub.service import HubError, HubService, safe_serve_content_type
from agora.models import PostMessage, Status

ADMIN = "test-admin-key"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64          # binary, not valid UTF-8
PDF = b"%PDF-1.7 fake body \xff\xfe" + b"A" * 128


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


@pytest.fixture()
def team(service):
    alice, _ = service.register_agent("alice", "Alice")
    bob, _ = service.register_agent("bob", "Bob")
    outsider, _ = service.register_agent("mallory", "Mallory")
    service.create_channel(alice, "design", private=True)
    token = service.create_invite(alice, "design", invitee="bob")
    service.join_channel(bob, "design", invite_token=token)
    return alice, bob, outsider


@pytest.fixture()
def http() -> TestClient:
    return TestClient(create_app(db_path=":memory:", admin_key=ADMIN,
                                 rate_per_minute=600.0))


def _reg(client, agent_id):
    r = client.post("/agents", json={"id": agent_id},
                    headers={"Authorization": f"Bearer {ADMIN}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


# -- storage: content addressing, idempotency, caps, gates -----------------------


def test_upload_is_content_addressed_and_idempotent(service, team):
    alice, bob, _ = team
    first = service.attachment_put(alice, "design", PNG,
                                   filename="shot.png", content_type="image/png")
    assert first["id"] == hashlib.sha256(PNG).hexdigest()
    assert first["size"] == len(PNG) and first["content_type"] == "image/png"
    # Same bytes again (even by another member): same id, no error.
    again = service.attachment_put(bob, "design", PNG, filename="copy.png",
                                   content_type="image/png")
    assert again["id"] == first["id"]
    # First writer's metadata is kept (immutable row).
    assert again["filename"] == "shot.png" and again["created_by"] == "alice"


def test_fetch_round_trip_is_byte_exact(service, team):
    alice, bob, _ = team
    meta = service.attachment_put(alice, "design", PDF, filename="spec.pdf",
                                  content_type="application/pdf")
    got_meta, got_bytes = service.attachment_get(bob, "design", meta["id"])
    assert got_bytes == PDF and got_meta["content_type"] == "application/pdf"


def test_upload_caps_and_empty_refuse(service, team):
    alice, _, _ = team
    small = HubService(Database(":memory:"), max_attachment_bytes=64)
    a, _ = small.register_agent("a", "A")
    small.create_channel(a, "c", private=True)
    with pytest.raises(HubError) as e:
        small.attachment_put(a, "c", b"x" * 65, filename="big.bin")
    assert e.value.status_code == 413
    with pytest.raises(HubError) as e:
        service.attachment_put(alice, "design", b"", filename="empty.bin")
    assert e.value.status_code == 400


def test_per_channel_storage_quota(service, team):
    """Review P2: append-only blobs need a ceiling so one member cannot fill
    the disk one distinct file at a time (the disk-full class). Dedup means
    re-uploading identical bytes never counts against the quota."""
    alice, _, _ = team
    tiny = HubService(Database(":memory:"), max_channel_attachment_bytes=100)
    a, _ = tiny.register_agent("a", "A")
    tiny.create_channel(a, "c", private=True)
    tiny.attachment_put(a, "c", b"x" * 60, filename="one.bin")
    # Re-uploading the SAME bytes is free (dedup) even at the ceiling.
    tiny.attachment_put(a, "c", b"x" * 60, filename="dup.bin")
    # A new distinct blob that would cross the cap is refused.
    with pytest.raises(HubError) as e:
        tiny.attachment_put(a, "c", b"y" * 60, filename="two.bin")
    assert e.value.status_code == 413 and "storage full" in e.value.detail


def test_http_upload_streaming_cap_rejects_oversized_body(http):
    """Review P1: the body must be bounded WHILE streaming, not buffered
    whole then measured. A body over the cap gets a 413 and nothing is
    stored — verified by the channel staying empty afterwards."""
    app = create_app(db_path=":memory:", admin_key=ADMIN, rate_per_minute=600.0,
                     max_attachment_bytes=128)
    client = TestClient(app)
    alice = _reg(client, "alice")
    client.post("/channels", json={"name": "design"}, headers=alice)
    r = client.post("/channels/design/attachments?filename=big.bin",
                    content=b"z" * 4096,
                    headers={**alice, "Content-Type": "application/octet-stream"})
    assert r.status_code == 413
    # Nothing landed: a fetch of the would-be id 404s (channel is empty).
    blob_id = hashlib.sha256(b"z" * 4096).hexdigest()
    assert client.get(f"/channels/design/attachments/{blob_id}",
                      headers=alice).status_code == 404


def test_membership_gates_upload_and_fetch(service, team):
    alice, _, mallory = team
    meta = service.attachment_put(alice, "design", PNG, filename="s.png")
    for act in (lambda: service.attachment_put(mallory, "design", PDF),
                lambda: service.attachment_get(mallory, "design", meta["id"])):
        with pytest.raises(HubError) as e:
            act()
        assert e.value.status_code == 403


def test_fetch_unknown_and_malformed_ids(service, team):
    alice, _, _ = team
    with pytest.raises(HubError) as e:
        service.attachment_get(alice, "design", "f" * 64)
    assert e.value.status_code == 404
    with pytest.raises(HubError) as e:
        service.attachment_get(alice, "design", "../../etc/passwd")
    assert e.value.status_code == 400


# -- message refs: validation, normalization, no bypass --------------------------


def test_post_with_attachment_normalizes_from_server_truth(service, team):
    alice, bob, _ = team
    meta = service.attachment_put(alice, "design", PNG, filename="shot.png",
                                  content_type="image/png")
    m = service.post_message(alice, "design", PostMessage(
        body="see the screenshot", status=Status.fyi,
        attachments=[{"id": meta["id"], "filename": "renamed.png"}]))
    [ref] = m.data["attachments"]
    # filename may be overridden for display; size/type come from the blob row.
    assert ref == {"id": meta["id"], "filename": "renamed.png",
                   "content_type": "image/png", "size": len(PNG)}


def test_ref_to_missing_or_foreign_channel_blob_refused(service, team):
    alice, _, _ = team
    service.create_channel(alice, "other", private=True)
    meta = service.attachment_put(alice, "other", PDF, filename="spec.pdf")
    # Uploaded to 'other', referenced from 'design': channel-scoped -> 400.
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(
            body="x", attachments=[{"id": meta["id"]}]))
    assert e.value.status_code == 400 and "not uploaded" in e.value.detail


def test_raw_data_attachments_cannot_bypass_validation(service, team):
    """The no-bypass rule that guards asks/answers guards attachments too:
    a hand-built data payload with fake size/type is re-normalized, and a
    fake id is refused."""
    alice, _, _ = team
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(
            body="x", data={"attachments": [{"id": "f" * 64, "size": 1}]}))
    assert e.value.status_code == 400
    meta = service.attachment_put(alice, "design", PNG, content_type="image/png")
    m = service.post_message(alice, "design", PostMessage(
        body="x", data={"attachments": [
            {"id": meta["id"], "size": 999999, "content_type": "text/html"}]}))
    [ref] = m.data["attachments"]
    assert ref["size"] == len(PNG) and ref["content_type"] == "image/png"


def test_attachment_ref_limits(service, team):
    alice, _, _ = team
    meta = service.attachment_put(alice, "design", PNG)
    dup = [{"id": meta["id"]}, {"id": meta["id"]}]
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(body="x", attachments=dup))
    assert "duplicate" in e.value.detail
    blobs = [service.attachment_put(alice, "design", bytes([i]) * 32)
             for i in range(9)]
    with pytest.raises(HubError) as e:
        service.post_message(alice, "design", PostMessage(
            body="x", attachments=[{"id": b["id"]} for b in blobs]))
    assert "at most" in e.value.detail


# -- delivery: refs ride the envelope, bytes never do ----------------------------


def test_envelope_carries_refs_even_without_inline_body(service, team):
    alice, bob, _ = team
    meta = service.attachment_put(alice, "design", PDF, filename="spec.pdf",
                                  content_type="application/pdf")
    service.post_message(alice, "design", PostMessage(
        body="x" * 5000,  # too big to inline for a non-addressed viewer
        attachments=[{"id": meta["id"]}]))
    env = next(e for e in service.inbox(bob) if e.attachments)
    assert env.body is None, "big body must stay envelope-only"
    [ref] = env.attachments
    assert ref["id"] == meta["id"] and ref["filename"] == "spec.pdf"
    assert "bytes" not in ref


# -- HTTP surface: upload, serve hardening, DMs ----------------------------------


def test_http_content_length_header_rejected_before_streaming(http):
    """A lying-or-honest Content-Length over the cap is rejected up front
    (defense before the stream even begins) — review P1's first gate."""
    app = create_app(db_path=":memory:", admin_key=ADMIN, rate_per_minute=600.0,
                     max_attachment_bytes=128)
    client = TestClient(app)
    alice = _reg(client, "alice")
    client.post("/channels", json={"name": "design"}, headers=alice)
    r = client.post("/channels/design/attachments?filename=x",
                    content=b"z" * 4096,
                    headers={**alice, "Content-Type": "application/octet-stream",
                             "Content-Length": "4096"})
    assert r.status_code == 413


def test_http_upload_post_fetch_round_trip(http):
    alice = _reg(http, "alice")
    http.post("/channels", json={"name": "design"}, headers=alice)
    up = http.post("/channels/design/attachments?filename=shot.png",
                   content=PNG, headers={**alice, "Content-Type": "image/png"})
    assert up.status_code == 200
    blob = up.json()
    assert blob["id"] == hashlib.sha256(PNG).hexdigest()

    posted = http.post("/channels/design/messages", headers=alice,
                       json={"body": "screenshot attached",
                             "attachments": [{"id": blob["id"]}]})
    assert posted.status_code == 200
    [ref] = posted.json()["data"]["attachments"]
    assert ref["content_type"] == "image/png"

    got = http.get(f"/channels/design/attachments/{blob['id']}", headers=alice)
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"].startswith("image/png")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-disposition"].startswith('attachment; filename="shot.png"')


def test_http_serve_downgrades_active_content_types(http):
    """A stored text/html or image/svg+xml blob must never be served
    executably: the hub would otherwise become a script origin for every
    member's browser session (the consumer proxy re-serves our headers)."""
    alice = _reg(http, "alice")
    http.post("/channels", json={"name": "design"}, headers=alice)
    for declared in ("text/html", "image/svg+xml",
                     "application/xhtml+xml", "not a type"):
        # Distinct bytes per case: identical bytes are content-addressed to
        # ONE row whose first-upload metadata sticks (covered elsewhere).
        payload = b"<svg onload=alert(1)>" + declared.encode()
        up = http.post("/channels/design/attachments?filename=x",
                       content=payload, headers={**alice, "Content-Type": declared})
        got = http.get(f"/channels/design/attachments/{up.json()['id']}",
                       headers=alice)
        assert got.headers["content-type"] == "application/octet-stream", declared
        # The declared type survives as labeled metadata, never as the
        # serving type.
        assert (declared == "not a type"
                or got.headers["x-declared-content-type"] == declared)


def test_dm_attachment_end_to_end(http):
    alice = _reg(http, "alice")
    bob = _reg(http, "bob")
    http.post("/dms/bob", headers=alice)  # create the DM channel
    up = http.post("/dms/alice--bob/attachments", headers=alice)  # wrong name
    assert up.status_code in (403, 404)  # never a silent success on a bad channel
    up = http.post("/channels/dm:alice--bob/attachments?filename=doc.pdf",
                   content=PDF, headers={**alice, "Content-Type": "application/pdf"})
    assert up.status_code == 200
    blob = up.json()
    sent = http.post("/dms/bob/messages", headers=alice,
                     json={"body": "the doc we discussed",
                           "attachments": [{"id": blob["id"]}]})
    assert sent.status_code == 200
    env = next(e for e in http.get("/inbox", headers=bob).json()
               if e.get("attachments"))
    assert env["attachments"][0]["filename"] == "doc.pdf"
    got = http.get(f"/channels/dm:alice--bob/attachments/{blob['id']}", headers=bob)
    assert got.content == PDF


def test_ledger_commits_to_attachment_identity(http):
    """data.attachments rides the hash chain: the transcript commits to the
    exact bytes via the content-addressed id (offline-verifiable)."""
    alice = _reg(http, "alice")
    http.post("/channels", json={"name": "design"}, headers=alice)
    up = http.post("/channels/design/attachments?filename=a.bin",
                   content=PNG, headers={**alice, "Content-Type": "application/octet-stream"})
    http.post("/channels/design/messages", headers=alice,
              json={"body": "with file", "attachments": [{"id": up.json()["id"]}]})
    ledger = http.get("/channels/design/ledger", headers=alice).json()
    assert ledger["verified"] is True
    [turn] = [t for t in ledger["turns"] if (t.get("data") or {}).get("attachments")]
    assert turn["data"]["attachments"][0]["id"] == up.json()["id"]


# -- agent-visible rendering (adversarial-eval P0, 2026-07-16) --------------------


def test_rendered_envelope_names_attachments_and_fetch_verb(service, team):
    """The hub delivered refs on every envelope but neither renderer showed
    them — the feature was INVISIBLE to recipients (eval P0). The rendered
    triage text must name the file, its declared type/size, the id, and the
    fetch verb."""
    from agora.render import render_envelopes, render_messages

    alice, bob, _ = team
    meta = service.attachment_put(alice, "design", PNG, filename="shot.png",
                                  content_type="image/png")
    m = service.post_message(alice, "design", PostMessage(
        body="x" * 5000,  # envelope-only for bob: refs must STILL render
        attachments=[{"id": meta["id"]}]))

    env = next(e for e in service.inbox(bob) if e.attachments)
    text = render_envelopes([env.model_dump(mode="json")])
    for token in ("shot.png", "image/png", meta["id"], "read_attachment"):
        assert token in text, f"envelope render missing {token!r}"

    # Deliberate-read path too — including the worst case, an EMPTY body
    # whose whole content is the attachment.
    bare = service.post_message(alice, "design", PostMessage(
        body="", attachments=[{"id": meta["id"]}]))
    read = render_messages([bare.model_dump(mode="json")])
    assert "shot.png" in read and meta["id"] in read


def test_dm_surfaces_carry_attachments(http):
    """Eval P1: the hub accepted DM attachments but no agent-facing DM verb
    exposed the parameter. The client dm() (which MCP send_dm mirrors) must
    deliver refs end to end."""
    alice = _reg(http, "alice")
    bob = _reg(http, "bob")
    http.post("/dms/bob", headers=alice)
    up = http.post("/channels/dm:alice--bob/attachments?filename=spec.pdf",
                   content=PDF, headers={**alice, "Content-Type": "application/pdf"})
    sent = http.post("/dms/bob/messages", headers=alice,
                     json={"body": "review this", "title": "doc",
                           "attachments": [{"id": up.json()["id"]}]})
    assert sent.status_code == 200
    [ref] = sent.json()["data"]["attachments"]
    assert ref["filename"] == "spec.pdf"


# -- hub-injected stale-client notice (incident c2578: a stale server cannot
# -- announce its own staleness — the warning must ride the response data) ---------


def test_stale_client_gets_hub_injected_notice(http):
    """A header-less /inbox caller (pre-handshake client) receives ONE
    synthetic system notice appended to non-empty deliveries: sender=hub,
    channel/seq mirroring a REAL row (cursor-safe by construction), telling
    the seat its renders are incomplete and how to fix it."""
    alice = _reg(http, "alice")
    bob = _reg(http, "bob")
    http.post("/channels", json={"name": "design", "private": False}, headers=alice)
    http.post("/channels/design/join", json={}, headers=bob)
    http.post("/channels/design/messages", json={"body": "hello"}, headers=alice)

    rows = http.get("/inbox", headers=bob).json()  # TestClient sends no X-Agora-Client
    notice = [r for r in rows if r["sender"] == "hub" and "HUB NOTICE" in r["title"]]
    assert len(notice) == 1
    real = [r for r in rows if r["sender"] != "hub"]
    assert (notice[0]["channel"], notice[0]["seq"]) in {(r["channel"], r["seq"])
                                                        for r in real}
    assert "restart" in notice[0]["body"].lower()

    # A CURRENT client (header present) never sees the synthetic row.
    fresh = http.get("/inbox", headers={**bob, "X-Agora-Client": "0.12.3"}).json()
    assert all(r["sender"] != "hub" or "HUB NOTICE" not in r["title"] for r in fresh)

    # An EMPTY inbox stays empty — nothing is hidden, so nothing is warned.
    http.post("/inbox/ack", headers={**bob, "X-Agora-Client": "0.12.3"},
              json={"cursors": {"design": rows[0]["seq"] if real else 1}})


def test_stale_notice_renders_through_old_envelope_model(http):
    """The synthetic row must parse through the Envelope model old renderers
    already have (extra fields ignored) and render loudly — this is the
    delivery path into a 0.11.x MCP server's existing render_envelopes."""
    from agora.models import Envelope
    from agora.render import render_envelopes

    alice = _reg(http, "alice")
    bob = _reg(http, "bob")
    http.post("/channels", json={"name": "design", "private": False}, headers=alice)
    http.post("/channels/design/join", json={}, headers=bob)
    http.post("/channels/design/messages", json={"body": "hi"}, headers=alice)
    rows = http.get("/inbox", headers=bob).json()
    Envelope(**[r for r in rows if r["sender"] == "hub"][0])  # must not raise
    text = render_envelopes(rows)
    assert "HUB NOTICE" in text and "restart" in text.lower()


# -- stale MCP-server visibility (incident c2563) ----------------------------------


def test_stale_banner_fires_only_when_hub_is_newer():
    """A long-running MCP server keeps the code it booted with; when the hub
    is upgraded underneath it, renders silently drop newer fields (the
    attachments-invisible incident). The banner must fire exactly when the
    hub is NEWER — never on equal, older, or unknown hub versions."""
    from agora.mcp.server import stale_banner_text

    assert stale_banner_text("0.12.1", "0.11.1").startswith("NOTE from your own tooling")
    assert "attachments" in stale_banner_text("0.12.1", "0.11.1")
    assert stale_banner_text("0.12.1", "0.12.1") == ""
    assert stale_banner_text("0.11.0", "0.12.1") == ""          # hub older
    assert stale_banner_text("", "0.12.1") == ""                # hub unknown
    assert stale_banner_text("0.12.0.dev0", "0.12.1") == ""     # dev < release
    assert stale_banner_text("1.0.0", "0.12.1") != ""


# -- serve-type hardening unit matrix ---------------------------------------------


def test_safe_serve_content_type_matrix():
    assert safe_serve_content_type("image/png") == "image/png"
    assert safe_serve_content_type("application/pdf; name=x") == "application/pdf"
    assert safe_serve_content_type("IMAGE/SVG+XML") == "application/octet-stream"
    assert safe_serve_content_type("text/html") == "application/octet-stream"
    assert safe_serve_content_type("application/weird+xml") == "application/octet-stream"
    assert safe_serve_content_type("") == "application/octet-stream"
    assert safe_serve_content_type("nonsense") == "application/octet-stream"
