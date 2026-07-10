"""Per-channel virtual filesystem (v0.5.0): the shared, network-accessible
'book' that lets agents on different machines edit a common workspace without a
shared disk. These tests exercise the service layer and the HTTP surface.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agora.db import Database, StoreConflict
from agora.hub.app import create_app
from agora.hub.service import HubError, HubService
from agora.models import Kind


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


# -- version archive (every edit's content is recoverable, with provenance) -----


def test_every_version_content_is_recoverable_with_provenance(service, agents):
    """The field question this answers: 'did they rewrite the plan or just
    sign it?' — any archived version reads back verbatim with its original
    author, so history is recoverable, not just countable."""
    alice, bob = agents
    service.fs_write(alice, "design", "plan.md", "# Plan by alice")
    service.fs_write(bob, "design", "plan.md", "# Plan by alice\n- bob: AGREED")
    service.fs_write(alice, "design", "plan.md", "# Plan v3 rewritten")

    v1 = service.fs_read(bob, "design", "plan.md", version=1)
    assert v1.content == "# Plan by alice" and v1.updated_by == "alice"
    v2 = service.fs_read(alice, "design", "plan.md", version=2)
    assert v2.content.endswith("bob: AGREED") and v2.updated_by == "bob"
    # Head is unaffected by archive reads.
    assert service.fs_read(bob, "design", "plan.md").version == 3


def test_missing_archive_version_is_a_clear_404(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "plan.md", "x")
    with pytest.raises(HubError) as e:
        service.fs_read(alice, "design", "plan.md", version=99)
    assert e.value.status_code == 404 and "archive" in e.value.detail


def test_deleted_version_reads_as_absent_but_prior_versions_survive(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "plan.md", "keep me")     # v1
    service.fs_delete(alice, "design", "plan.md")               # v2 tombstone
    with pytest.raises(HubError):
        service.fs_read(alice, "design", "plan.md", version=2)  # delete = absent
    assert service.fs_read(alice, "design", "plan.md", version=1).content == "keep me"


def test_archive_read_requires_membership(service, agents):
    alice, _ = agents
    outsider, _ = service.register_agent("eve", "Eve")
    service.fs_write(alice, "design", "plan.md", "secret")
    with pytest.raises(HubError) as e:
        service.fs_read(outsider, "design", "plan.md", version=1)
    assert e.value.status_code == 403


def test_http_fs_read_version_param():
    app = create_app(db_path=":memory:", admin_key="t", rate_per_minute=600.0)
    client = TestClient(app)
    r = client.post("/agents", json={"id": "a"},
                    headers={"Authorization": "Bearer t"})
    auth = {"Authorization": f"Bearer {r.json()['api_key']}"}
    client.post("/channels", json={"name": "design"}, headers=auth)
    client.put("/channels/design/fs/plan.md", json={"content": "v1 text"},
               headers=auth)
    client.put("/channels/design/fs/plan.md", json={"content": "v2 text"},
               headers=auth)
    old = client.get("/channels/design/fs/plan.md?version=1", headers=auth).json()
    assert old["content"] == "v1 text" and old["version"] == 1
    head = client.get("/channels/design/fs/plan.md", headers=auth).json()
    assert head["content"] == "v2 text" and head["version"] == 2


# -- descriptions (the listing is a table of contents, not a path dump) ---------


def test_listing_carries_writer_description(service, agents):
    alice, bob = agents
    service.fs_write(alice, "design", "plans/topo.md", "# Topology\nbody",
                     description="consensus plan: 1 gateway + N runtimes")
    [row] = service.fs_list(bob, "design")
    assert row["description"] == "consensus plan: 1 gateway + N runtimes"
    assert row["described"] is True
    assert row["size"] == len("# Topology\nbody")
    # The read surface carries it too.
    assert service.fs_read(bob, "design", "plans/topo.md").description \
        == "consensus plan: 1 gateway + N runtimes"


def test_listing_derives_description_when_writer_set_none(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "notes.md",
                     "\n\n# Seam notes for v2\nlong body...")
    [row] = service.fs_list(alice, "design")
    assert row["description"] == "Seam notes for v2"   # first line, de-markdowned
    assert row["described"] is False                   # marked as derived


def test_description_is_sanitized_and_capped(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "x.md", "body",
                     description="  spaced\n\nout  " + "y" * 500)
    [row] = service.fs_list(alice, "design")
    assert row["description"].startswith("spaced out")
    assert len(row["description"]) <= 200


# -- core lifecycle ------------------------------------------------------------


def test_write_read_list_delete(service, agents):
    alice, bob = agents
    f = service.fs_write(alice, "design", "docs/plan.md", "# Plan\nline")
    assert f.version == 1 and f.path == "docs/plan.md" and f.size_bytes > 0
    # A peer on the same channel sees and reads it (shared workspace).
    assert service.fs_read(bob, "design", "docs/plan.md").content == "# Plan\nline"
    assert [x["path"] for x in service.fs_list(bob, "design")] == ["docs/plan.md"]
    assert service.fs_delete(bob, "design", "docs/plan.md") is True
    assert service.fs_list(bob, "design") == []
    with pytest.raises(HubError) as e:
        service.fs_read(bob, "design", "docs/plan.md")
    assert e.value.status_code == 404


def test_cas_prevents_lost_update(service, agents):
    alice, bob = agents
    v1 = service.fs_write(alice, "design", "c.md", "a", expect_version=0)
    # A concurrent editor with a stale expectation is refused, not silently clobbered.
    with pytest.raises(StoreConflict) as e:
        service.fs_write(bob, "design", "c.md", "b", expect_version=0)
    assert e.value.current_version == 1
    v2 = service.fs_write(bob, "design", "c.md", "b", expect_version=v1.version)
    assert v2.version == 2 and service.fs_read(alice, "design", "c.md").content == "b"


def test_delete_cas(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "c.md", "x")  # version 1
    with pytest.raises(StoreConflict):
        service.fs_delete(alice, "design", "c.md", expect_version=99)
    assert service.fs_delete(alice, "design", "c.md", expect_version=1) is True


def test_list_prefix(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "docs/a.md", "1")
    service.fs_write(alice, "design", "docs/b.md", "2")
    service.fs_write(alice, "design", "src/c.py", "3")
    docs = sorted(x["path"] for x in service.fs_list(alice, "design", prefix="docs/"))
    assert docs == ["docs/a.md", "docs/b.md"]


# -- audit trail (replayable history over an append-only log) -------------------


def test_history_records_put_and_delete(service, agents):
    alice, _ = agents
    service.fs_write(alice, "design", "p.md", "one")
    service.fs_write(alice, "design", "p.md", "two")
    service.fs_delete(alice, "design", "p.md")
    hist = service.fs_history(alice, "design", "p.md")
    ops = [(m.data["op"], m.data["version"]) for m in hist]
    # Version is monotonic across the whole lifetime (delete tombstones at v3).
    assert ops == [("put", 1), ("put", 2), ("delete", 3)]
    assert all(m.kind == Kind.fs.value for m in hist)


def test_version_is_monotonic_across_delete_recreate_no_aba(service, agents):
    """Independent-tester finding: a stale pre-delete version must NOT pass CAS
    after the file is deleted and recreated (ABA). Because delete tombstones and
    the version never resets, the pre-delete holder is correctly refused."""
    alice, bob = agents
    v1 = service.fs_write(alice, "design", "doc.md", "a", expect_version=0)
    assert v1.version == 1
    service.fs_delete(bob, "design", "doc.md", expect_version=1)      # tombstone -> v2
    recreated = service.fs_write(bob, "design", "doc.md", "fresh", expect_version=0)  # -> v3
    assert recreated.version == 3, "recreate must continue the monotonic sequence, not reset to 1"
    # alice, still holding her pre-delete version 1, must not clobber the recreated file.
    with pytest.raises(StoreConflict) as e:
        service.fs_write(alice, "design", "doc.md", "clobber", expect_version=1)
    assert e.value.current_version == 3
    assert service.fs_read(alice, "design", "doc.md").content == "fresh"


def test_recreate_after_delete_with_create_semantics(service, agents):
    """A deleted path reads as absent, and can be re-created with the create
    guard (expect_version=0) — the version just continues monotonically."""
    alice, _ = agents
    service.fs_write(alice, "design", "r.md", "one")   # v1
    service.fs_delete(alice, "design", "r.md")          # v2 tombstone
    with pytest.raises(HubError) as e:
        service.fs_read(alice, "design", "r.md")
    assert e.value.status_code == 404
    again = service.fs_write(alice, "design", "r.md", "two", expect_version=0)
    assert again.version == 3 and service.fs_read(alice, "design", "r.md").content == "two"
    assert [x["path"] for x in service.fs_list(alice, "design")] == ["r.md"]


# -- boundaries: path safety, size, membership, store-guard --------------------


@pytest.mark.parametrize("bad", [
    "/etc/passwd", "../secret", "a/../b", "a//b", "a/./b", "", "  ", "a/ b",
    "x" * 600, "a\\b", "a\x00b",
])
def test_path_traversal_and_junk_rejected(service, agents, bad):
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", bad, "x")
    assert e.value.status_code == 400


def test_size_cap(service, agents):
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.fs_write(alice, "design", "big.md", "x" * (256 * 1024 + 1))
    assert e.value.status_code == 413


def test_non_member_cannot_touch_fs(service, agents):
    eve, _ = service.register_agent("eve", "Eve")  # not a member of "design"
    for call in (
        lambda: service.fs_write(eve, "design", "x.md", "1"),
        lambda: service.fs_read(eve, "design", "x.md"),
        lambda: service.fs_list(eve, "design"),
        lambda: service.fs_delete(eve, "design", "x.md"),
    ):
        with pytest.raises(HubError) as e:
            call()
        assert e.value.status_code == 403


def test_fs_keys_are_not_writable_via_store(service, agents):
    """Files must only be mutated through the fs_* API (which validates and emits
    an audit event); a raw store_set to an fs/ key is refused."""
    alice, _ = agents
    with pytest.raises(HubError) as e:
        service.store_set(alice, "design", "fs/sneaky.md", {"content": "x"})
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


def test_http_fs_roundtrip_with_nested_path(http):
    alice = _reg(http, "alice")
    http.post("/channels", json={"name": "design"}, headers=alice)
    # write a nested path (catch-all route must preserve slashes)
    w = http.put("/channels/design/fs/docs/deep/plan.md",
                 json={"content": "# hi", "expect_version": 0}, headers=alice)
    assert w.status_code == 200 and w.json()["version"] == 1
    r = http.get("/channels/design/fs/docs/deep/plan.md", headers=alice)
    assert r.json()["content"] == "# hi"
    assert http.get("/channels/design/fs", headers=alice).json()[0]["path"] == "docs/deep/plan.md"
    # stale CAS over HTTP -> 409
    conflict = http.put("/channels/design/fs/docs/deep/plan.md",
                        json={"content": "x", "expect_version": 0}, headers=alice)
    assert conflict.status_code == 409
    d = http.delete("/channels/design/fs/docs/deep/plan.md", headers=alice)
    assert d.json()["deleted"] is True


def test_http_fs_outsider_rejected(http):
    alice = _reg(http, "alice")
    eve = _reg(http, "eve")
    http.post("/channels", json={"name": "design"}, headers=alice)
    assert http.get("/channels/design/fs", headers=eve).status_code == 403
    assert http.put("/channels/design/fs/x.md", json={"content": "1"},
                    headers=eve).status_code == 403
