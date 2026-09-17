"""Real VFS publication -> durable subscription -> inbox and listener boundary."""
import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agora.db import Database, StoreConflict
from agora.hub.app import create_app
from agora.hub.collaboration_graph import collaboration_graph
from agora.hub.notify_sink import NotifySink
from agora.hub.service import HubError, HubService
from agora.listen import parse_line, qualifies
from agora.models import PostMessage


@pytest.fixture
def fleet(tmp_path):
    db = Database(str(tmp_path / 'hub.db'))
    hub = HubService(db, rate_per_minute=10000, notify_sink=NotifySink(tmp_path / 'notify'))
    seats = [hub.register_agent(name, name)[0] for name in ('author', 'reader', 'bystander', 'outsider')]
    hub.create_channel(seats[0], 'work', private=True)
    for seat in seats[1:3]:
        hub.join_channel(seat, 'work', invite_token=hub.create_invite(seats[0], 'work', invitee=seat.id))
    yield hub, seats, tmp_path
    db.close()


def notices(hub):
    return [m for m in hub.db.get_messages('work', limit=10000) if (m.data or {}).get('vfs_trigger')]


def test_mutation_reaches_only_subscribed_attention_and_creates_no_debt(fleet):
    hub, (author, reader, bystander, outsider), root = fleet
    sub = hub.fs_subscribe(reader, 'work', 'plan.md')
    assert sub['after_version'] == 0
    hub.fs_write(author, 'work', 'plan.md', 'Current assumptions', summary='Separate storage from transport')
    [notice] = notices(hub)
    assert notice.to == [reader.id] and notice.status.value == 'fyi'
    assert notice.data['vfs_trigger']['event'] == 'created'
    assert notice.data['vfs_trigger']['summary'] == 'Separate storage from transport'
    lines = [parse_line(s) for s in (root / 'notify/reader-inbox.log').read_text().splitlines()]
    event = next(e for e in lines if e['id'] == notice.id)
    assert qualifies(event, reader.id, important_only=True)
    other = [parse_line(s) for s in (root / 'notify/bystander-inbox.log').read_text().splitlines()]
    assert not qualifies(next(e for e in other if e['id'] == notice.id), bystander.id, important_only=True)
    assert notice.id not in [r.id for r in hub.owed(reader).to_answer]
    with pytest.raises(HubError):
        hub.get_message_by_seq(outsider, 'work', notice.seq)
    assert hub.db.verify_channel('work')['ok']
    graph = collaboration_graph(hub, author, 'work')
    assert any(e['relation'] == 'subscribes_to' and e['source'] == 'seat:reader' for e in graph['edges'])
    assert any(e['relation'] == 'reports_revision' and e['version'] == 1 for e in graph['edges'])


@pytest.mark.parametrize('urgency,wakes', [('inbox', False), ('next_turn', True), ('interrupt', True)])
def test_subscriber_controls_attention_not_work_obligation(fleet, urgency, wakes):
    hub, (author, reader, *_), root = fleet
    hub.fs_subscribe(reader, 'work', 'a.md', urgency=urgency)
    hub.fs_write(author, 'work', 'a.md', 'x')
    [notice] = notices(hub)
    event = next(parse_line(s) for s in (root / 'notify/reader-inbox.log').read_text().splitlines()
                 if json.loads(s)['id'] == notice.id)
    assert qualifies(event, reader.id, important_only=True) is wakes
    assert not hub.owed(reader).to_answer


def test_delete_recreate_filtering_and_original_author_echo(fleet):
    hub, (author, reader, *_), _ = fleet
    hub.fs_subscribe(reader, 'work', 'a.md', events=['created', 'deleted'])
    hub.fs_subscribe(author, 'work', 'a.md')
    hub.fs_write(author, 'work', 'a.md', 'first')
    hub.fs_write(author, 'work', 'a.md', 'second')
    hub.fs_delete(author, 'work', 'a.md')
    assert hub.fs_delete(author, 'work', 'a.md') is False
    hub.fs_write(author, 'work', 'a.md', 'reborn', expect_version=0)
    rows = notices(hub)
    assert [(m.data['vfs_trigger']['event'], m.data['vfs_trigger']['version']) for m in rows] == [
        ('created', 1), ('deleted', 3), ('created', 4)]
    assert all(m.to == [reader.id] for m in rows)
    deletion = rows[1].data['vfs_trigger']
    assert deletion['read']['arguments']['version'] == 2
    assert hub.fs_read(reader, **deletion['read']['arguments']).content == 'second'
    assert all(s['after_version'] == 4 for s in hub.fs_subscriptions(reader, 'work'))


def test_subscribe_is_future_only_and_retries_preserve_pending_events(fleet, monkeypatch):
    hub, (author, reader, *_), _ = fleet
    hub.fs_write(author, 'work', 'a.md', 'old')
    first = hub.fs_subscribe(reader, 'work', 'a.md')
    assert first['after_version'] == 1 and not notices(hub)
    monkeypatch.setattr(hub, '_fs_trigger_after_write', lambda: None)
    hub.fs_write(author, 'work', 'a.md', 'new')
    assert hub.fs_subscribe(reader, 'work', 'a.md')['id'] == first['id']
    assert hub.fs_subscribe(reader, 'work', 'a.md')['after_version'] == 1
    hub._fs_trigger_sweep()
    assert [m.data['vfs_trigger']['version'] for m in notices(hub)] == [2]
    hub.fs_write(author, 'work', 'a.md', 'third')
    changed = hub.fs_subscribe(reader, 'work', 'a.md', urgency='inbox')
    assert changed['id'] != first['id'] and changed['after_version'] == 3
    assert hub._fs_trigger_sweep() == []


def test_crash_between_revision_and_audit_recovers_after_restart(fleet, monkeypatch):
    hub, (author, reader, *_), root = fleet
    hub.fs_subscribe(reader, 'work', 'a.md')
    monkeypatch.setattr(hub, '_post_fs_audit', lambda *a: (_ for _ in ()).throw(RuntimeError('simulated crash')))
    with pytest.raises(RuntimeError):
        hub.fs_write(author, 'work', 'a.md', 'committed', summary='Durable before notification')
    assert hub.fs_read(reader, 'work', 'a.md').content == 'committed'
    assert not notices(hub)
    db2 = Database(str(root / 'hub.db'))
    restarted = HubService(db2, notify_sink=NotifySink(root / 'notify'))
    try:
        assert len(restarted._fs_trigger_sweep()) == 1
        [notice] = notices(restarted)
        assert notice.data['vfs_trigger']['summary'] == 'Durable before notification'
        assert restarted._fs_trigger_sweep() == []
    finally:
        db2.close()


def test_crash_after_publication_before_cursor_replays_same_notice(fleet, monkeypatch):
    hub, (author, reader, *_), root = fleet
    hub.fs_subscribe(reader, 'work', 'a.md')
    original = hub.db.fs_subscription_advance
    monkeypatch.setattr(hub.db, 'fs_subscription_advance', lambda *a: (_ for _ in ()).throw(RuntimeError('crash')))
    hub.fs_write(author, 'work', 'a.md', 'x')  # artifact succeeds; cursor remains pending
    [first] = notices(hub)
    assert hub.fs_subscriptions(reader, 'work')[0]['after_version'] == 0
    monkeypatch.setattr(hub.db, 'fs_subscription_advance', original)
    assert hub._fs_trigger_sweep() == [first.id]
    assert len(notices(hub)) == 1
    delivered = [json.loads(s) for s in (root / 'notify/reader-inbox.log').read_text().splitlines()]
    assert sum(e['id'] == first.id for e in delivered) == 2
    assert hub.db.verify_channel('work')['ok']


def test_notify_disk_failure_retains_cursor_and_retries(fleet, monkeypatch):
    hub, (author, reader, *_), _ = fleet
    hub.fs_subscribe(reader, 'work', 'a.md')
    original = hub.notify_sink._append
    def failed(path, line):
        if path.name == 'reader-inbox.log':
            raise OSError('disk full')
        original(path, line)
    monkeypatch.setattr(hub.notify_sink, '_append', failed)
    hub.fs_write(author, 'work', 'a.md', 'x')
    [first] = notices(hub)
    assert hub.fs_subscriptions(reader, 'work')[0]['after_version'] == 0
    monkeypatch.setattr(hub.notify_sink, '_append', original)
    assert hub._fs_trigger_sweep() == [first.id]
    assert len(notices(hub)) == 1
    assert hub.fs_subscriptions(reader, 'work')[0]['after_version'] == 1


def test_failed_recipient_cannot_starve_the_bounded_queue(fleet, monkeypatch):
    hub, (author, reader, bystander, _), _ = fleet
    hub.fs_subscribe(reader, 'work', 'a.md')
    hub.fs_subscribe(bystander, 'work', 'a.md')
    monkeypatch.setattr(hub, '_fs_trigger_after_write', lambda: None)
    hub.fs_write(author, 'work', 'a.md', 'new')
    pending = hub.db.fs_pending_subscriptions
    monkeypatch.setattr(hub.db, 'fs_pending_subscriptions', lambda: pending(limit=1))
    deliver = hub.notify_sink.deliver
    monkeypatch.setattr(hub.notify_sink, 'deliver',
                        lambda who, message: False if who == reader.id else deliver(who, message))
    assert hub._fs_trigger_sweep() == []
    assert len(hub._fs_trigger_sweep()) == 1
    rows = {s['agent_id']: s for s in hub.fs_subscriptions(author, 'work')}
    assert rows[reader.id]['after_version'] == 0  # no fabricated progress
    assert rows[bystander.id]['after_version'] == 1


def test_short_notify_writes_form_complete_parseable_lines(fleet, monkeypatch):
    hub, (author, reader, *_), root = fleet
    hub.fs_subscribe(reader, 'work', 'a.md')
    real_write = os.write
    monkeypatch.setattr(os, 'write', lambda fd, data: real_write(fd, data[:max(1, len(data)//2)]))
    hub.fs_write(author, 'work', 'a.md', 'x')
    [notice] = notices(hub)
    lines = [parse_line(s) for s in (root / 'notify/reader-inbox.log').read_text().splitlines()]
    assert all(lines)
    assert qualifies(next(e for e in lines if e['id'] == notice.id), reader.id, important_only=True)
    assert hub.fs_subscriptions(reader, 'work')[0]['after_version'] == 1


def test_failed_partial_notify_write_does_not_corrupt_retry(tmp_path, monkeypatch):
    sink = NotifySink(tmp_path)
    path = tmp_path / 'reader-inbox.log'
    sink._append(path, '{"old":true}')
    original = path.read_bytes()
    real_write = os.write
    calls = 0
    def partial_failure(fd, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('disk full after partial write')
        return real_write(fd, data[:3])
    monkeypatch.setattr(os, 'write', partial_failure)
    with pytest.raises(OSError):
        sink._append(path, '{"new":true}')
    assert path.read_bytes() == original
    monkeypatch.setattr(os, 'write', real_write)
    sink._append(path, '{"new":true}')
    assert [json.loads(s) for s in path.read_text().splitlines()] == [{'old': True}, {'new': True}]


def test_restarted_notify_sink_recovers_from_killed_partial_writer(fleet, monkeypatch):
    hub, (author, reader, *_), root = fleet
    hub.fs_subscribe(reader, 'work', 'a.md')
    monkeypatch.setattr(hub, '_fs_trigger_after_write', lambda: None)
    hub.fs_write(author, 'work', 'a.md', 'new')
    path = root / 'notify/reader-inbox.log'
    with path.open('ab') as stream:
        stream.write(b'{"channel":"work","id":"unfinished')
    hub.notify_sink = NotifySink(root / 'notify')
    hub._fs_trigger_sweep()
    [notice] = notices(hub)
    lines = [parse_line(s) for s in path.read_text().splitlines()]
    assert all(lines)
    assert qualifies(next(e for e in lines if e['id'] == notice.id), reader.id, important_only=True)
    assert hub.fs_subscriptions(reader, 'work')[0]['after_version'] == 1


@pytest.mark.parametrize('cancel', ['unsubscribe', 'leave'])
def test_pending_notice_does_not_survive_unsubscribe_or_membership_loss(fleet, monkeypatch, cancel):
    hub, (author, reader, *_), _ = fleet
    hub.fs_subscribe(reader, 'work', 'a.md')
    monkeypatch.setattr(hub, '_fs_trigger_after_write', lambda: None)
    hub.fs_write(author, 'work', 'a.md', 'private')
    if cancel == 'unsubscribe':
        assert hub.fs_unsubscribe(reader, 'work', 'a.md')['unsubscribed']
    else:
        hub.leave_channel(reader, 'work')
    assert not hub._fs_trigger_sweep() and not notices(hub)
    hub.fs_unsubscribe(reader, 'work', 'a.md')  # legal even after access loss
    if cancel == 'leave':
        hub.join_channel(reader, 'work', invite_token=hub.create_invite(author, 'work', invitee=reader.id))
        assert not hub.fs_subscriptions(reader, 'work')


def test_normal_asks_remain_separate_and_subscribe_cannot_clear_dependencies(fleet):
    hub, (author, reader, *_), _ = fleet
    source = hub.post_message(author, 'work', PostMessage(status='open',
        asks=[{'id': 'review', 'text': 'Check whether the interface preserves cancellation.', 'to': ['reader']}]))
    hub.fs_subscribe(reader, 'work', 'interface.md')
    hub.fs_write(author, 'work', 'interface.md', 'draft')
    assert source.id in [r.id for r in hub.owed(reader).to_answer]
    hub.fs_unsubscribe(reader, 'work', 'interface.md')
    assert source.id in [r.id for r in hub.owed(reader).to_answer]
    hub.post_message(reader, 'work', PostMessage(status='reply', reply_to=source.id,
        answers=['review'], body='The new interface loses cancellation; retain the parent signal.'))
    assert source.id not in [r.id for r in hub.owed(reader).to_answer]


def test_workspace_and_attachments_are_not_vfs_events_and_cas_failure_is_quiet(fleet):
    hub, (author, reader, *_), root = fleet
    hub.fs_subscribe(reader, 'work', 'a.md')
    (root / 'a.md').write_text('local')
    hub.attachment_put(author, 'work', b'attachment', filename='a.md')
    assert hub._fs_trigger_sweep() == [] and not notices(hub)
    hub.fs_write(author, 'work', 'a.md', 'vfs')
    with pytest.raises(StoreConflict):
        hub.fs_write(author, 'work', 'a.md', 'wrong', expect_version=0)
    assert len(notices(hub)) == 1


@pytest.mark.parametrize('options', [{'path': '../x'}, {'events': []}, {'events': ['updated', 'updated']},
                                   {'events': ['approved']}, {'urgency': 'critical'}])
def test_invalid_subscriptions_never_mutate(fleet, options):
    hub, (_, reader, *_), _ = fleet
    with pytest.raises(HubError):
        hub.fs_subscribe(reader, 'work', **{'path': 'a.md', **options})
    assert hub.fs_subscriptions(reader, 'work') == []


def test_http_identity_binding_and_private_visibility():
    with TestClient(create_app(db_path=':memory:', admin_key='test', rate_per_minute=10000)) as client:
        auth = {}
        for name in ('a', 'b'):
            r = client.post('/agents', json={'id': name}, headers={'Authorization': 'Bearer test'})
            auth[name] = {'Authorization': 'Bearer ' + r.json()['api_key']}
        assert client.post('/channels', json={'name': 'private', 'private': True}, headers=auth['a']).status_code == 200
        route = '/channels/private/fs-subscriptions/plan.md'
        assert client.put(route, json={}, headers=auth['b']).status_code == 403
        assert client.get('/channels/private/fs-subscriptions', headers=auth['b']).status_code == 403
        assert client.put(route, json={'agent_id': 'b'}, headers=auth['a']).status_code == 422
        row = client.put(route, json={}, headers=auth['a']).json()
        assert row['agent_id'] == 'a'
        assert client.delete(route, headers=auth['b']).json()['unsubscribed'] is False
        assert len(client.get('/channels/private/fs-subscriptions', headers=auth['a']).json()) == 1


def test_migration_preserves_old_archive_without_replaying_history(tmp_path):
    path = str(tmp_path / 'old.db')
    db = Database(path)
    hub = HubService(db)
    author, _ = hub.register_agent('author', 'author')
    hub.fs_write(author, 'commons', 'a.md', 'old')
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE fs_versions DROP COLUMN event_type')
        conn.execute('ALTER TABLE fs_versions DROP COLUMN summary')
        conn.execute('DROP TABLE fs_subscriptions')
    db = Database(path)
    try:
        hub = HubService(db)
        assert hub.fs_read(author, 'commons', 'a.md', version=1).content == 'old'
        sub = hub.fs_subscribe(author, 'commons', 'a.md')
        assert sub['after_version'] == 1
        assert hub._fs_trigger_sweep() == []
    finally:
        db.close()
