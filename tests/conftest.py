

# The repository is itself a workspace with a `.agora/seat.json` (the `agora`
# seat on the operator's hub). Since 2026-09-09 a workspace seat record is the
# CLI's and the MCP server's default hub, so a test that runs from the repo
# root would otherwise resolve the operator's hub. Tests that want a seat file
# write their own and chdir to it.
import pytest as _pytest
from pathlib import Path as _Path


@_pytest.fixture(autouse=True)
def _no_repo_seat_file(monkeypatch):
    from agora import setup_harness as _sh
    repo = _Path(__file__).resolve().parents[1]
    real = _sh.read_workspace_seat

    def guarded(workspace):
        try:
            if _Path(workspace).resolve() == repo:
                return None
        except OSError:
            pass
        return real(workspace)
    monkeypatch.setattr(_sh, "read_workspace_seat", guarded)
