"""Seat-local working copies of authoritative VFS text, bound to their edit base.

No local path comes from the caller. The hub still owns authorization and CAS;
this client helper keeps a resumed context from substituting a newer head number
for the version from which its local edit was actually materialized.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from urllib.parse import quote

from ..models import MAX_STORE_VALUE_BYTES


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _metadata(row: dict) -> dict:
    # Publishing must not echo the entire just-supplied artifact into context.
    return {k: v for k, v in row.items() if k not in ('content', 'content_b64')}


class Checkouts:
    def __init__(self, root: Path, call):
        self.root = root
        self.call = call

    def checkout(self, channel: str, path: str) -> dict:
        row = self.call('GET', f"/channels/{quote(channel, safe='')}/fs/{quote(path, safe='/')}")
        if not isinstance(row, dict) or row.get('ok') is False:
            return row
        if row.get('encoding') == 'base64' or not isinstance(row.get('content'), str):
            raise ValueError('fs_checkout edits text only; binary files use attachments or fs_read')
        checkout_id = uuid.uuid4().hex
        directory = self.root / checkout_id
        directory.mkdir(parents=True, mode=0o700)
        suffix = Path(row['path']).suffix
        # Fixed basename: supplied artifact names cannot become local rules or
        # configuration files, and the caller cannot choose a write destination.
        filename = 'working' + (suffix if re.fullmatch(r'\.[a-zA-Z0-9]{1,12}', suffix) else '.txt')
        content = row['content']
        receipt = {'channel': channel, 'path': row['path'], 'version': row['version'],
                   'sha256': _sha(content), 'mime': row['mime'], 'working_file': filename}
        (directory / 'base.txt').write_bytes(content.encode('utf-8'))
        (directory / filename).write_bytes(content.encode('utf-8'))
        (directory / 'receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
        return {'checkout_id': checkout_id, 'file_path': str(directory / filename),
                'base_file': str(directory / 'base.txt'), 'base_version': row['version'],
                'base_sha256': receipt['sha256'], 'channel': channel, 'path': row['path'],
                'next': 'Edit file_path with native tools, then fs_publish(checkout_id). '
                        'This checkout keeps its original base across contexts; do not refresh its receipt.'}

    @staticmethod
    def _read(directory_fd: int, name: str, limit: int = MAX_STORE_VALUE_BYTES) -> bytes:
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError('checkout files must remain regular files inside their original directory')
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise ValueError('checkout files must remain regular files inside their original directory') from exc
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError('checkout files must remain regular files inside their original directory')
        with os.fdopen(fd, 'rb') as stream:
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError('edited text exceeds the 256 KiB VFS limit; preserve the local '
                                 'work and use logical sections or a workspace-authoritative artifact')
            return data

    def publish(self, checkout_id: str, summary: str = "") -> dict:
        if not isinstance(checkout_id, str) or not re.fullmatch(r'[0-9a-f]{32}', checkout_id):
            raise ValueError('invalid checkout_id; use the ID returned by fs_checkout')
        directory = self.root / checkout_id
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise ValueError('checkout directory must remain inside its original root')
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                receipt = json.loads(self._read(fd, 'receipt.json', 8192))
                if (not isinstance(receipt, dict)
                        or any(not isinstance(receipt.get(k), str) for k in
                               ('channel', 'path', 'mime', 'working_file', 'sha256'))
                        or type(receipt.get('version')) is not int or receipt['version'] < 1):
                    raise ValueError('malformed checkout receipt; make a fresh checkout and reconcile')
                base = self._read(fd, 'base.txt').decode('utf-8')
                content = self._read(fd, receipt['working_file']).decode('utf-8')
            finally:
                os.close(fd)
        except FileNotFoundError as exc:
            raise ValueError('checkout not found for this hub/credential binding; after a key change '
                             'make a fresh fs_checkout and reconcile the preserved old working file') from exc
        if _sha(base) != receipt['sha256']:
            raise ValueError('checkout base was modified; create a fresh checkout and reconcile your edits')
        route = f"/channels/{quote(receipt['channel'], safe='')}/fs/{quote(receipt['path'], safe='/')}"
        current = self.call('GET', route)
        if not isinstance(current, dict) or current.get('ok') is False:
            return current
        if current.get('encoding') == 'base64':
            return {'ok': False, 'error': 409, 'detail': 'Current artifact is binary; local edits are preserved.'}
        # Retrying the exact submitted bytes is a no-op, including after a lost
        # HTTP reply. This never advances the saved base for a later edit.
        if current['content'] == content:
            return {**_metadata(current), 'unchanged': True, 'checkout_id': checkout_id}
        if current['version'] != receipt['version'] or _sha(current['content']) != receipt['sha256']:
            return {'ok': False, 'error': 409, 'base_version': receipt['version'],
                    'current_version': current['version'], 'checkout_id': checkout_id,
                    'detail': 'The authoritative artifact changed since this checkout. Both versions '
                              'are preserved. Make a fresh fs_checkout, reconcile the local changes '
                              'against its current contents, then publish that new checkout. '
                              'Do not replace the new working file wholesale with the stale copy.'}
        # Compare actual bytes against the captured base, not a supplied head
        # integer. CAS also protects the race after the GET above.
        diff = ''.join(difflib.unified_diff(base.splitlines(True), content.splitlines(True),
                                          fromfile=f"base@{receipt['version']}", tofile='working'))
        # A fresh exclusive diff file cannot follow a substituted existing link.
        diff_path = directory / ('changes-' + uuid.uuid4().hex + '.diff')
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            diff_fd = os.open(diff_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=directory_fd)
            with os.fdopen(diff_fd, 'w', encoding='utf-8') as stream:
                stream.write(diff)
        finally:
            os.close(directory_fd)
        result = self.call('PUT', route, json={'content': content, 'mime': receipt['mime'],
                                              'expect_version': receipt['version'], 'summary': summary})
        if isinstance(result, dict) and result.get('ok') is not False:
            return {**_metadata(result), 'checkout_id': checkout_id, 'base_version': receipt['version'],
                    'base_sha256': receipt['sha256'], 'diff_path': str(diff_path),
                    'next': 'Published. For another edit, start a fresh checkout of the current artifact.'}
        return result
