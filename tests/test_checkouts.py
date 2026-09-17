"""Real VFS and two independent local contexts reproduce the stale-base incident."""
import json
import os
from pathlib import Path

import pytest

from agora.db import Database, StoreConflict
from agora.hub.service import HubError, HubService
from agora.mcp.checkouts import Checkouts


@pytest.fixture
def setup(tmp_path):
    hub = HubService(Database(':memory:'), rate_per_minute=600)
    seat, _ = hub.register_agent('editor', 'Editor')
    hub.create_channel(seat, 'work', private=True)
    hub.fs_write(seat, 'work', 'draft.md', 'Opening\n\nOriginal ending\n', description='Shared draft')

    def call(method, route, **kwargs):
        _, _, channel, _, path = route.split('/', 4)
        try:
            row = (hub.fs_read(seat, channel, path) if method == 'GET'
                   else hub.fs_write(seat, channel, path, **kwargs['json']))
            return row.model_dump()
        except HubError as exc:
            return {'ok': False, 'error': exc.status_code, 'detail': exc.detail}
        except StoreConflict as exc:
            return {'ok': False, 'error': 409, 'detail': str(exc)}

    return hub, seat, Checkouts(tmp_path / 'checkouts', call), call


def edit(checkout, text):
    Path(checkout['file_path']).write_bytes(text.encode('utf-8'))


def test_two_contexts_cannot_publish_a_stale_copy_with_a_new_head(setup):
    hub, seat, checkouts, call = setup
    reception = checkouts.checkout('work', 'draft.md')
    work = checkouts.checkout('work', 'draft.md')
    edit(work, 'Opening\n\nExpanded ending with accepted work\n')
    result = checkouts.publish(work['checkout_id'])
    assert result['version'] == 2 and 'content' not in result
    assert Path(result['diff_path']).read_text().endswith('+Expanded ending with accepted work\n')
    # Resumed context knows the current version but its working copy is old.
    resumed = Checkouts(checkouts.root, call)
    assert hub.fs_read(seat, 'work', 'draft.md').version == 2
    edit(reception, 'New opening\n\nOriginal ending\n')
    refused = resumed.publish(reception['checkout_id'])
    assert refused['error'] == 409 and refused['base_version'] == 1
    assert refused['current_version'] == 2
    assert 'Expanded ending' in hub.fs_read(seat, 'work', 'draft.md').content
    assert 'New opening' in Path(reception['file_path']).read_text()
    assert 'Original ending' in Path(reception['base_file']).read_text()
    # Explicit reconciliation preserves the new contribution and accepted work.
    fresh = resumed.checkout('work', 'draft.md')
    edit(fresh, 'New opening\n\nExpanded ending with accepted work\n')
    assert resumed.publish(fresh['checkout_id'])['version'] == 3
    assert hub.fs_read(seat, 'work', 'draft.md').description == 'Shared draft'


def test_retries_do_not_advance_the_original_base(setup):
    hub, seat, checkouts, _ = setup
    c = checkouts.checkout('work', 'draft.md')
    assert checkouts.publish(c['checkout_id'])['unchanged'] is True
    edit(c, 'Revised\n')
    assert checkouts.publish(c['checkout_id'])['version'] == 2
    assert checkouts.publish(c['checkout_id'])['unchanged'] is True
    edit(c, 'Another edit\n')
    assert checkouts.publish(c['checkout_id'])['error'] == 409
    assert hub.fs_read(seat, 'work', 'draft.md').version == 2


def test_cas_protects_concurrent_change_after_base_check(setup):
    hub, seat, checkouts, call = setup
    c = checkouts.checkout('work', 'draft.md')
    edit(c, 'Local edit')

    def racing_call(method, route, **kwargs):
        if method == 'PUT':
            hub.fs_write(seat, 'work', 'draft.md', 'Peer update', expect_version=1)
        return call(method, route, **kwargs)

    assert Checkouts(checkouts.root, racing_call).publish(c['checkout_id'])['error'] == 409
    assert hub.fs_read(seat, 'work', 'draft.md').content == 'Peer update'


def test_same_version_but_different_base_bytes_is_not_accepted(setup):
    _, _, checkouts, call = setup
    c = checkouts.checkout('work', 'draft.md')
    edit(c, 'Local edit')

    def altered_read(method, route, **kwargs):
        assert method == 'GET'
        return {**call(method, route, **kwargs), 'content': 'Different bytes at same version'}

    assert Checkouts(checkouts.root, altered_read).publish(c['checkout_id'])['error'] == 409


def test_base_tampering_missing_and_cross_binding_ids_are_loud(setup, tmp_path):
    _, _, checkouts, call = setup
    c = checkouts.checkout('work', 'draft.md')
    with pytest.raises(ValueError, match='not found'):
        Checkouts(tmp_path / 'other-binding', call).publish(c['checkout_id'])
    Path(c['base_file']).write_text('Changed base')
    with pytest.raises(ValueError, match='base was modified'):
        checkouts.publish(c['checkout_id'])
    Path(c['base_file']).unlink()
    with pytest.raises(ValueError, match='not found'):
        checkouts.publish(c['checkout_id'])


@pytest.mark.parametrize('name', ['../receipt', '/tmp/escape', 'g' * 32, ''])
def test_id_cannot_choose_local_paths(setup, name):
    with pytest.raises(ValueError, match='invalid checkout_id'):
        setup[2].publish(name)


def test_symlink_and_receipt_path_escape_refused(setup, tmp_path):
    _, _, checkouts, _ = setup
    c = checkouts.checkout('work', 'draft.md')
    path = Path(c['file_path'])
    outside = tmp_path / 'outside'; outside.write_text('Private')
    path.unlink(); path.symlink_to(outside)
    with pytest.raises(ValueError, match='regular files'):
        checkouts.publish(c['checkout_id'])
    path.unlink(); path.write_text('Edit')
    receipt = path.parent / 'receipt.json'
    value = json.loads(receipt.read_text()); value['working_file'] = str(outside)
    receipt.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='regular files'):
        checkouts.publish(c['checkout_id'])


def test_fresh_checkout_preserves_previous_edits_and_raw_authority(setup):
    hub, seat, checkouts, _ = setup
    old = checkouts.checkout('work', 'draft.md'); edit(old, 'Unpublished local work')
    hub.fs_write(seat, 'work', 'draft.md', 'Raw VFS update', expect_version=1)
    fresh = checkouts.checkout('work', 'draft.md')
    assert fresh['file_path'] != old['file_path']
    assert Path(old['file_path']).read_text() == 'Unpublished local work'
    assert Path(fresh['file_path']).read_text() == 'Raw VFS update'
    assert checkouts.publish(old['checkout_id'])['error'] == 409


def test_hub_refusals_and_binary_files_are_not_materialized(setup):
    hub, seat, checkouts, _ = setup
    assert checkouts.checkout('missing', 'draft.md')['ok'] is False
    assert not checkouts.root.exists()
    hub.fs_write(seat, 'work', 'binary.png', content_b64='AA==')
    with pytest.raises(ValueError, match='text only'):
        checkouts.checkout('work', 'binary.png')


def test_oversized_edit_stays_local(setup):
    hub, seat, checkouts, _ = setup
    c = checkouts.checkout('work', 'draft.md')
    edit(c, 'x' * (256 * 1024 + 1))
    with pytest.raises(ValueError, match='256 KiB'):
        checkouts.publish(c['checkout_id'])
    assert hub.fs_read(seat, 'work', 'draft.md').version == 1
    assert Path(c['file_path']).stat().st_size == 256 * 1024 + 1


@pytest.mark.parametrize('kind', ['directory', 'fifo'])
def test_non_regular_working_file_does_not_block_publish(setup, kind):
    _, _, checkouts, _ = setup
    c = checkouts.checkout('work', 'draft.md')
    path = Path(c['file_path']); path.unlink()
    if kind == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(ValueError, match='regular files'):
        checkouts.publish(c['checkout_id'])


@pytest.mark.parametrize('receipt', [[], {}, {'working_file': 12}])
def test_malformed_receipt_is_loud(setup, receipt):
    _, _, checkouts, _ = setup
    c = checkouts.checkout('work', 'draft.md')
    (Path(c['file_path']).parent / 'receipt.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='malformed checkout receipt'):
        checkouts.publish(c['checkout_id'])
