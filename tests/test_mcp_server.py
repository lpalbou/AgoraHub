import importlib
import sys
from pathlib import Path


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
