"""Binary entries in the per-channel virtual file system (vfs): operators deposit
images/PDFs/etc. as base64 (`content_b64`) so agents can reference them by fs
path. Exactly one of `content`/`content_b64` per write; binary bytes never
ride the `content` field (old clients see an empty string, not base64 soup);
caps, listings and audits speak DECODED bytes. These tests exercise the
service layer and the HTTP surface, mirroring tests/test_fs.py.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from agora.db import Database, StoreConflict
from agora.hub.app import create_app
from agora.hub.service import HubError, HubService

try:
    from agora.models import MAX_FS_BINARY_BYTES
except ImportError:  # a stale INSTALLED agora shadowing src/ (same root cause
    # as test_fs.py's TextTooLong import failure): the imported package
    # predates fs binary support, so these tests cannot run against it. Skip
    # LOUDLY rather than fail collection — run under the project venv
    # (`uv run pytest`) to exercise the checkout.
    pytest.skip("imported 'agora' predates fs binary support (stale installed "
                "copy shadowing the checkout — run tests via the project venv)",
                allow_module_level=True)

PNG_ISH = bytes(range(256)) * 4          # not valid UTF-8: a true byte payload
PNG_ISH_B64 = base64.b64encode(PNG_ISH).decode()


@pytest.fixture()
def service() -> HubService:
    return HubService(Database(":memory:"), rate_per_minute=600.0)


@pytest.fixture()
def agents(service):
    alice, _ = service.register_agent("alice", "Alice")
    bob, _ = service.register_agent("bob", "Bob")
    service.create_channel(alice, "design", private=True)
    token = service.create_invite(alice, "design", invitee="bob")
    service.join_channel(bob, "design", invite_token=token)
    return alice, bob


def _authored(rows: list) -> list:
    """Drop the hub-owned `channel/` files (the seeded charter is scenery)."""
    return [r for r in rows if not r["path"].startswith("channel/")]


# -- round-trip ----------------------------------------------------------------


def test_binary_roundtrip_with_encoding_marker_and_default_mime(service, agents):
    alice, bob = agents
    w = service.fs_write(alice, "design", "assets/logo.png",
                         content_b64=PNG_ISH_B64)
    assert w.version == 1
    assert w.encoding == "base64" and w.content_b64 == PNG_ISH_B64
    assert w.content == ""                       # never the base64
    assert w.size_bytes == len(PNG_ISH)          # DECODED bytes
    # Omitted mime arrives as the pydantic text default; for bytes the
    # default flips to octet-stream.
    assert w.mime == "application/octet-stream"

    r = service.fs_read(bob, "design", "assets/logo.png")
    assert base64.b64decode(r.content_b64) == PNG_ISH    # bytes survive verbatim
    assert r.encoding == "base64" and r.content == ""
    assert r.mime == "application/octet-stream"
    assert r.size_bytes == len(PNG_ISH)


def test_binary_explicit_mime_is_kept(service, agents):
    alice, _ = agents
    w = service.fs_write(alice, "design", "scan.pdf", content_b64=PNG_ISH_B64,
                         mime="application/pdf", description="signed contract scan")
    assert w.mime == "application/pdf"
    r = service.fs_read(alice, "design", "scan.pdf")
    assert r.mime == "application/pdf" and r.description == "signed contract scan"


def test_text_default_mime_unchanged(service, agents):
    alice, _ = agents
    assert service.fs_write(alice, "design", "a.md", "text").mime == "text/markdown"


def test_binary_audit_records_decoded_size(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "blob.bin", content_b64=PNG_ISH_B64)
    [audit] = service.fs_history(alice, "design", "blob.bin")
    assert audit.data["size_bytes"] == len(PNG_ISH)  # decoded, not b64 length


# -- exactly one of content / content_b64 --------------------------------------


def test_both_fields_rejected(service, agents):
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "x.md", "text", content_b64=PNG_ISH_B64)
    assert e.value.status_code == 400
    assert "exactly one of content or content_b64" in e.value.detail


def test_neither_field_rejected(service, agents):
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "x.md")
    assert e.value.status_code == 400
    assert "exactly one of content or content_b64" in e.value.detail


@pytest.mark.parametrize("bad", [
    "not base64!!",       # alphabet violation
    "abc",                # length not a multiple of 4
    "AAA=AAA=",           # padding in the middle
    "YQ==\n",             # newline: strict validation, no whitespace laundering
])
def test_invalid_base64_rejected(service, agents, bad):
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "x.bin", content_b64=bad)
    assert e.value.status_code == 400
    assert "content_b64 is not valid base64" in e.value.detail


def test_decoded_size_cap_413(service, agents):
    alice, _ = agents
    over = base64.b64encode(b"\x00" * (MAX_FS_BINARY_BYTES + 1)).decode()
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "big.bin", content_b64=over)
    assert e.value.status_code == 413
    assert f"exceeds {MAX_FS_BINARY_BYTES} bytes" in e.value.detail
    # Exactly at the cap is accepted (decoded bytes, not base64 length).
    at_cap = base64.b64encode(b"\x00" * MAX_FS_BINARY_BYTES).decode()
    assert service.fs_write(alice, "design", "big.bin",
                            content_b64=at_cap).size_bytes == MAX_FS_BINARY_BYTES


# -- CAS -----------------------------------------------------------------------


def test_cas_create_only_conflict_on_binary(service, agents):
    alice, bob = agents
    v1 = service.fs_write(alice, "design", "c.bin", content_b64=PNG_ISH_B64,
                          expect_version=0)
    with pytest.raises(StoreConflict) as e:
        service.fs_write(bob, "design", "c.bin", content_b64=PNG_ISH_B64,
                         expect_version=0)
    assert e.value.current_version == 1
    v2 = service.fs_write(bob, "design", "c.bin",
                          content_b64=base64.b64encode(b"v2").decode(),
                          expect_version=v1.version)
    assert v2.version == 2


# -- text <-> binary overwrites on one path, with archived reads ---------------


def test_text_binary_overwrite_and_archived_version_reads(service, agents):
    alice, bob = agents
    service.fs_write(alice, "design", "doc", "# plain text v1")        # v1 text
    service.fs_write(bob, "design", "doc", content_b64=PNG_ISH_B64,
                     mime="image/png")                                 # v2 binary
    service.fs_write(alice, "design", "doc", "text again v3")          # v3 text

    v1 = service.fs_read(bob, "design", "doc", version=1)
    assert v1.content == "# plain text v1" and v1.encoding is None
    assert v1.content_b64 is None and v1.updated_by == "alice"

    v2 = service.fs_read(alice, "design", "doc", version=2)
    assert v2.encoding == "base64" and v2.content == ""
    assert base64.b64decode(v2.content_b64) == PNG_ISH
    assert v2.mime == "image/png" and v2.updated_by == "bob"
    assert v2.size_bytes == len(PNG_ISH)

    head = service.fs_read(bob, "design", "doc")
    assert head.version == 3 and head.content == "text again v3"
    assert head.encoding is None and head.content_b64 is None


# -- listings ------------------------------------------------------------------


def test_fs_list_marks_binary_with_decoded_size(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "notes.md", "# Notes\nbody")
    service.fs_write(alice, "design", "img.png", content_b64=PNG_ISH_B64,
                     description="the logo")
    service.fs_write(alice, "design", "empty.md", "")   # ambiguity guard: text, size 0
    rows = {r["path"]: r for r in _authored(service.fs_list(alice, "design"))}

    binary = rows["img.png"]
    assert binary["encoding"] == "base64"
    assert binary["size"] == len(PNG_ISH)               # decoded bytes
    assert binary["description"] == "the logo" and binary["described"] is True

    text = rows["notes.md"]
    assert "encoding" not in text
    assert text["size"] == len("# Notes\nbody")

    empty = rows["empty.md"]                            # empty TEXT stays text
    assert "encoding" not in empty and empty["size"] == 0


def test_zero_byte_binary_is_allowed_and_listed(service, agents):
    alice, _ = agents
    w = service.fs_write(alice, "design", "empty.bin", content_b64="")
    assert w.size_bytes == 0 and w.encoding == "base64"
    [row] = [r for r in _authored(service.fs_list(alice, "design"))
             if r["path"] == "empty.bin"]
    assert row["encoding"] == "base64" and row["size"] == 0


# -- governance boundaries stay intact -----------------------------------------


def test_charter_refuses_binary_even_from_owner(service, agents):
    alice, _ = agents   # alice OWNS "design": authority is not the blocker here
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "channel/charter.md",
                         content_b64=PNG_ISH_B64)
    assert e.value.status_code == 400
    assert "charter must be text" in e.value.detail


def test_reserved_prefix_authority_enforced_for_binary(service, agents):
    _, bob = agents     # bob is a plain member, not the owner
    with pytest.raises(HubError) as e:
        service.fs_write(bob, "design", "channel/logo.png",
                         content_b64=PNG_ISH_B64)
    assert e.value.status_code == 403


def test_non_member_cannot_binary_write(service, agents):
    eve, _ = service.register_agent("eve", "Eve")
    with pytest.raises(HubError) as e:
        service.fs_write(eve, "design", "x.bin", content_b64=PNG_ISH_B64)
    assert e.value.status_code == 403


# -- HTTP surface --------------------------------------------------------------

ADMIN = "test-admin-key"


@pytest.fixture()
def http() -> TestClient:
    return TestClient(create_app(db_path=":memory:", admin_key=ADMIN, rate_per_minute=600.0))


def _reg(client, agent_id):
    r = client.post("/agents", json={"id": agent_id},
                    headers={"Authorization": f"Bearer {ADMIN}"})
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def test_http_binary_roundtrip_and_archived_version(http):
    alice = _reg(http, "alice")
    http.post("/channels", json={"name": "design"}, headers=alice)
    w = http.put("/channels/design/fs/assets/logo.png",
                 json={"content_b64": PNG_ISH_B64, "expect_version": 0},
                 headers=alice)
    assert w.status_code == 200
    body = w.json()
    assert body["encoding"] == "base64" and body["content"] == ""
    assert body["mime"] == "application/octet-stream"
    assert body["size_bytes"] == len(PNG_ISH)

    r = http.get("/channels/design/fs/assets/logo.png", headers=alice).json()
    assert base64.b64decode(r["content_b64"]) == PNG_ISH

    # Overwrite with text; the archived binary version still reads verbatim.
    http.put("/channels/design/fs/assets/logo.png",
             json={"content": "replaced by text"}, headers=alice)
    old = http.get("/channels/design/fs/assets/logo.png?version=1",
                   headers=alice).json()
    assert old["encoding"] == "base64"
    assert base64.b64decode(old["content_b64"]) == PNG_ISH
    head = http.get("/channels/design/fs/assets/logo.png", headers=alice).json()
    assert head["content"] == "replaced by text" and head["encoding"] is None

    listing = _authored(http.get("/channels/design/fs", headers=alice).json())
    assert listing[0]["size"] == len("replaced by text")


def test_http_exactly_one_content_field(http):
    alice = _reg(http, "alice")
    http.post("/channels", json={"name": "design"}, headers=alice)
    both = http.put("/channels/design/fs/x.bin",
                    json={"content": "a", "content_b64": "YQ=="}, headers=alice)
    assert both.status_code == 400
    neither = http.put("/channels/design/fs/x.bin", json={}, headers=alice)
    assert neither.status_code == 400
    bad = http.put("/channels/design/fs/x.bin",
                   json={"content_b64": "not base64!!"}, headers=alice)
    assert bad.status_code == 400


def test_render_fs_file_marks_binary_instead_of_empty_fence():
    # An MCP fs_read of a binary entry must say what it is — an empty fenced
    # body read as "empty text file", indistinguishable and misleading.
    from agora.render import render_fs_file
    out = render_fs_file({"path": "assets/logo.png", "version": 2, "mime": "image/png",
                          "encoding": "base64", "content": "", "content_b64": "aGk=",
                          "size_bytes": 2, "updated_by": "laurent", "updated_at": 1.0})
    assert "binary file — image/png, 2 bytes" in out
    assert "aGk=" not in out  # base64 payload never rides the fence
