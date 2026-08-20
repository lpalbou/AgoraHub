"""Binary files in the channel virtual file system (vfs) — client + CLI side.

The wire contract (fixed): PUT /channels/{c}/fs/{path} takes exactly ONE of
`content` (text, unchanged) or `content_b64` (strict standard base64, decoded
cap 4 MiB, default mime application/octet-stream); binary reads come back as
`content` == "" plus `content_b64` and `encoding` == "base64". These tests pin
the CLIENT half: the request body shape (exactly-one-of, b64 correctness, the
text body byte-identical to before), fs_read passing binary fields through
untouched, and the CLI's text-vs-binary autodetection and tty refusal.

Mock HTTP only — no network, no live hub (same pattern as
test_client_delivery.py / test_cli_surfaces.py's stubs).
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys

import pytest

from agora.client.client import AgoraClient

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256))  # not valid UTF-8
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


# ---------------------------------------------------------------------------
# client: request/response shape against a recording HTTP stub
# ---------------------------------------------------------------------------


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _RecordingHTTP:
    """httpx.AsyncClient stand-in: records calls, returns a canned payload."""

    def __init__(self, payload=None):
        self.calls: list[dict] = []
        self._payload = payload if payload is not None else {}

    async def put(self, path, json=None, **kw):
        self.calls.append({"method": "PUT", "path": path, "json": json})
        return _Resp(self._payload)

    async def get(self, path, params=None, **kw):
        self.calls.append({"method": "GET", "path": path, "params": params})
        return _Resp(self._payload)


def _client(http) -> AgoraClient:
    client = AgoraClient("http://test", "key")
    client._http = http  # type: ignore[assignment]
    return client


def test_fs_write_text_body_unchanged():
    """The text path is the pre-binary wire body, byte for byte: `content` +
    the text/markdown default, never a content_b64 key."""
    http = _RecordingHTTP({"version": 1, "size_bytes": 5})
    asyncio.run(_client(http).fs_write("design", "notes.md", "hello"))
    assert http.calls == [{
        "method": "PUT", "path": "/channels/design/fs/notes.md",
        "json": {"content": "hello", "mime": "text/markdown",
                 "expect_version": None, "description": ""},
    }]


def test_fs_write_binary_body_and_b64_correctness():
    """A binary write sends `content_b64` (no `content` key) that strictly
    decodes back to the original bytes; explicit mime/CAS ride along."""
    http = _RecordingHTTP({"version": 1, "size_bytes": len(PNG_BYTES)})
    asyncio.run(_client(http).fs_write(
        "design", "assets/logo.png", content_b64=PNG_B64, mime="image/png",
        expect_version=0, description="the logo"))
    body = http.calls[0]["json"]
    assert body == {"content_b64": PNG_B64, "mime": "image/png",
                    "expect_version": 0, "description": "the logo"}
    assert base64.b64decode(body["content_b64"], validate=True) == PNG_BYTES


def test_fs_write_binary_defaults_to_octet_stream():
    http = _RecordingHTTP({"version": 1, "size_bytes": len(PNG_BYTES)})
    asyncio.run(_client(http).fs_write("design", "blob", content_b64=PNG_B64))
    assert http.calls[0]["json"]["mime"] == "application/octet-stream"


def test_fs_write_exactly_one_of_content_and_b64():
    """Both or neither must fail CLIENT-side (mirrors the hub's 400) before
    anything travels."""
    http = _RecordingHTTP()
    client = _client(http)
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(client.fs_write("design", "x", "text", content_b64=PNG_B64))
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(client.fs_write("design", "x"))
    assert http.calls == []


def test_fs_read_passes_binary_fields_through_untouched():
    """fs_read must surface content_b64/encoding exactly as served — decoding
    is the caller's move, so the client never rewrites the row."""
    row = {"path": "assets/logo.png", "content": "", "content_b64": PNG_B64,
           "encoding": "base64", "mime": "image/png", "version": 3,
           "size_bytes": len(PNG_BYTES)}
    got = asyncio.run(_client(_RecordingHTTP(row)).fs_read(
        "design", "assets/logo.png"))
    assert got == row
    assert base64.b64decode(got["content_b64"]) == PNG_BYTES


# ---------------------------------------------------------------------------
# CLI: autodetection and the binary-read guard, over a fake client
# ---------------------------------------------------------------------------


class _FakeClient:
    """AgoraClient stand-in for cmd_fs: records fs_write args, serves one
    canned fs_read row."""

    def __init__(self, read_row=None):
        self.writes: list[dict] = []
        self._read_row = read_row or {}

    async def fs_write(self, channel, path, content=None, *, content_b64=None,
                       mime=None, expect_version=None, description=""):
        self.writes.append({
            "channel": channel, "path": path, "content": content,
            "content_b64": content_b64, "mime": mime,
            "expect_version": expect_version, "description": description})
        size = (len(content.encode("utf-8")) if content is not None
                else len(base64.b64decode(content_b64)))
        return {"version": 1, "size_bytes": size}

    async def fs_read(self, channel, path, version=None):
        return self._read_row


def _run_fs(monkeypatch, argv: list[str], client: _FakeClient) -> None:
    from agora import cli

    args = cli.build_parser().parse_args(argv)
    monkeypatch.setattr(cli, "_run_agent_cmd",
                        lambda a, coro_fn: asyncio.run(coro_fn(client, a)))
    args.func(args)


def test_cli_write_utf8_file_goes_as_content(tmp_path, monkeypatch):
    src = tmp_path / "notes.md"
    src.write_text("# hello\n", encoding="utf-8")
    client = _FakeClient()
    _run_fs(monkeypatch, ["fs", "--channel", "design", "--as", "bob",
                          "write", "notes.md", "--file", str(src)], client)
    assert client.writes == [{
        "channel": "design", "path": "notes.md", "content": "# hello\n",
        "content_b64": None, "mime": None, "expect_version": None,
        "description": ""}]


def test_cli_write_png_autodetects_binary_with_guessed_mime(tmp_path,
                                                            monkeypatch):
    src = tmp_path / "logo.png"
    src.write_bytes(PNG_BYTES)
    client = _FakeClient()
    _run_fs(monkeypatch, ["fs", "--channel", "design", "--as", "bob",
                          "write", "assets/logo.png", "--file", str(src)],
            client)
    (w,) = client.writes
    assert w["content"] is None
    assert base64.b64decode(w["content_b64"], validate=True) == PNG_BYTES
    assert w["mime"] == "image/png"


def test_cli_write_binary_flag_forces_b64_on_valid_utf8(tmp_path, monkeypatch):
    src = tmp_path / "notes.txt"
    src.write_text("plain text", encoding="utf-8")
    client = _FakeClient()
    _run_fs(monkeypatch, ["fs", "--channel", "design", "--as", "bob",
                          "write", "notes.txt", "--file", str(src),
                          "--binary"], client)
    (w,) = client.writes
    assert w["content"] is None
    assert base64.b64decode(w["content_b64"]) == b"plain text"
    assert w["mime"] == "text/plain"  # guessed from the .txt path


def test_cli_write_over_cap_refuses_with_fix(tmp_path, monkeypatch):
    """4 MiB + 1 of non-text must refuse locally, naming the fix (attachment),
    before 5+ MiB of base64 travels."""
    src = tmp_path / "huge.bin"
    src.write_bytes(b"\xff" * (4 * 1024 * 1024 + 1))
    client = _FakeClient()
    with pytest.raises(SystemExit, match="4 MiB"):
        _run_fs(monkeypatch, ["fs", "--channel", "design", "--as", "bob",
                              "write", "huge.bin", "--file", str(src)], client)
    with pytest.raises(SystemExit, match="attach it to a message"):
        _run_fs(monkeypatch, ["fs", "--channel", "design", "--as", "bob",
                              "write", "huge.bin", "--file", str(src)], client)
    assert client.writes == []


_BINARY_ROW = {"path": "assets/logo.png", "content": "", "content_b64": PNG_B64,
               "encoding": "base64", "mime": "image/png", "version": 3,
               "size_bytes": len(PNG_BYTES)}


def test_cli_read_binary_writes_decoded_bytes_to_out(tmp_path, monkeypatch,
                                                     capsys):
    out = tmp_path / "logo.png"
    _run_fs(monkeypatch, ["fs", "--channel", "design", "--as", "bob",
                          "read", "assets/logo.png", "--out", str(out)],
            _FakeClient(read_row=_BINARY_ROW))
    assert out.read_bytes() == PNG_BYTES
    meta = capsys.readouterr().out
    assert "assets/logo.png" in meta and "v3" in meta
    assert "image/png" in meta and str(len(PNG_BYTES)) in meta


def test_cli_read_binary_refuses_tty_without_out(monkeypatch):
    """Raw bytes must never hit a terminal: on a tty with no --out the read
    refuses and names the flag."""

    class _TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdout", _TTY())
    with pytest.raises(SystemExit, match="--out"):
        _run_fs(monkeypatch, ["fs", "--channel", "design", "--as", "bob",
                              "read", "assets/logo.png"],
                _FakeClient(read_row=_BINARY_ROW))


def test_cli_read_text_unchanged(monkeypatch, capsys):
    _run_fs(monkeypatch, ["fs", "--channel", "design", "--as", "bob",
                          "read", "notes.md"],
            _FakeClient(read_row={"path": "notes.md", "content": "# hello",
                                  "mime": "text/markdown", "version": 2}))
    assert capsys.readouterr().out == "# hello\n"
