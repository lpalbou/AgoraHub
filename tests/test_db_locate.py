"""The db-path decision matrix (db_locate) — every row, plus the cmd_up
persistence-ordering pins.

What we want (born from the a2a->agora rename incident, 2026-07-27): an
EXPLICIT --db may create a new database; a REMEMBERED path (config.json,
$AGORA_DB) may only ever open an existing one; the default stays
zero-friction for fresh installs; every refusal is a named diagnosis with
remedies, exit 3, and config.json is only rewritten after a successful
open. These tests illustrate the contract — the logic in db_locate must
hold for any paths, not just these examples.
"""

import json
import os
from pathlib import Path

import pytest

from agora import cli
from agora import db_locate


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Point AGORA_HOME at an empty dir; scrub every ambient override that
    could leak the developer's real machine state into the matrix."""
    home = tmp_path / "home"
    monkeypatch.setenv("AGORA_HOME", str(home))
    monkeypatch.delenv("AGORA_DB", raising=False)
    monkeypatch.delenv("AGORA_URL", raising=False)
    monkeypatch.delenv("AGORA_ADMIN_KEY", raising=False)
    return home


def _default(home: Path) -> str:
    return str(home / "agora.db")


def _mkdb(path: Path, size: int = 32) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# -- resolve(): source classification and normalization -----------------------

def test_fresh_install_default_boots_silently(isolated_home):
    home = isolated_home
    r = db_locate.resolve(None, None, None, _default(home))
    assert r.source == db_locate.DEFAULT
    notices = db_locate.preflight_up(r, home=home, default=_default(home),
                                     config_exists=False)
    assert notices == []


def test_default_missing_with_config_prints_new_db_notice(isolated_home):
    home = isolated_home
    r = db_locate.resolve(None, None, None, _default(home))
    notices = db_locate.preflight_up(r, home=home, default=_default(home),
                                     config_exists=True)
    assert len(notices) == 1 and "NEW EMPTY hub db" in notices[0]


def test_config_equal_to_default_reclassified(isolated_home):
    # A remembered path that IS the default is the default: deleting the
    # default db to reset must boot fresh, never hit the moved-project
    # refusal. Realpath equality is the contract.
    home = isolated_home
    r = db_locate.resolve(None, None, _default(home), _default(home))
    assert r.source == db_locate.DEFAULT


def test_config_symlinked_spelling_still_default(isolated_home):
    home = isolated_home
    home.mkdir(parents=True, exist_ok=True)
    alias = home.parent / "alias-home"
    alias.symlink_to(home)
    r = db_locate.resolve(None, None, str(alias / "agora.db"),
                          _default(home))
    assert r.source == db_locate.DEFAULT


def test_flag_beats_env(isolated_home):
    home = isolated_home
    r = db_locate.resolve("/flag/agora.db", "/env/agora.db", None,
                          _default(home))
    assert r.source == db_locate.FLAG and r.path == "/flag/agora.db"


def test_flag_normalized_expanduser_abspath(isolated_home, monkeypatch,
                                            tmp_path):
    home = isolated_home
    monkeypatch.chdir(tmp_path)
    r = db_locate.resolve("var/agora.db", None, None, _default(home))
    assert r.path == str(tmp_path / "var" / "agora.db")
    r2 = db_locate.resolve("~/somewhere/agora.db", None, None,
                           _default(home))
    assert r2.path == str(Path.home() / "somewhere" / "agora.db")


def test_memory_db_refused(isolated_home, capsys):
    with pytest.raises(SystemExit) as ex:
        db_locate.resolve(":memory:", None, None, _default(isolated_home))
    assert ex.value.code == 3
    assert "':memory:' is for tests" in capsys.readouterr().err


def test_config_relative_db_path_refuses(isolated_home, capsys):
    with pytest.raises(SystemExit) as ex:
        db_locate.resolve(None, None, "var/agora.db",
                          _default(isolated_home))
    assert ex.value.code == 3
    assert "RELATIVE" in capsys.readouterr().err


# -- preflight_up(): the matrix rows ------------------------------------------

def test_config_path_missing_refuses_with_diagnosis(isolated_home, capsys):
    home = isolated_home
    gone = home / "moved-project" / "var" / "agora.db"
    r = db_locate.resolve(None, None, str(gone), _default(home))
    with pytest.raises(SystemExit) as ex:
        db_locate.preflight_up(r, home=home, default=_default(home),
                               config_exists=True)
    assert ex.value.code == 3
    err = capsys.readouterr().err
    assert "REFUSING to start:" in err
    assert str(gone) in err                      # names the remembered path
    assert "moved or renamed" in err             # names the likely cause
    assert "--db /real/path" in err              # remedy 1
    assert _default(home) in err                 # remedy 2 (adopt default)
    assert "nothing was changed" in err


def test_refusal_inventory_names_default_and_backup(isolated_home, capsys):
    home = isolated_home
    _mkdb(Path(_default(home)), size=1024)
    snap = home / "backups" / "agora-20260727-230841.db"
    _mkdb(snap)
    r = db_locate.resolve(None, None, str(home / "gone" / "agora.db"),
                          _default(home))
    with pytest.raises(SystemExit):
        db_locate.preflight_up(r, home=home, default=_default(home),
                               config_exists=True)
    err = capsys.readouterr().err
    assert "default location:" in err and _default(home) in err
    assert "newest snapshot:" in err and str(snap) in err


def test_env_agora_db_missing_refuses_naming_variable(isolated_home, capsys):
    home = isolated_home
    r = db_locate.resolve(None, str(home / "stale" / "agora.db"), None,
                          _default(home))
    assert r.source == db_locate.ENV
    with pytest.raises(SystemExit) as ex:
        db_locate.preflight_up(r, home=home, default=_default(home),
                               config_exists=True)
    assert ex.value.code == 3
    err = capsys.readouterr().err
    assert "$AGORA_DB" in err and "unset AGORA_DB" in err


def test_flag_missing_parent_refuses_named_parent(isolated_home, capsys):
    home = isolated_home
    target = home / "no-such-dir" / "agora.db"
    r = db_locate.resolve(str(target), None, None, _default(home))
    with pytest.raises(SystemExit) as ex:
        db_locate.preflight_up(r, home=home, default=_default(home),
                               config_exists=True)
    assert ex.value.code == 3
    assert str(target.parent) in capsys.readouterr().err
    assert not target.parent.exists()            # nothing was created


def test_flag_missing_parent_exists_creates_with_notice(isolated_home):
    home = isolated_home
    parent = home / "chosen"
    parent.mkdir(parents=True)
    r = db_locate.resolve(str(parent / "agora.db"), None, None,
                          _default(home))
    notices = db_locate.preflight_up(r, home=home, default=_default(home),
                                     config_exists=True)
    assert len(notices) == 1 and "NEW hub db" in notices[0]


def test_db_path_is_directory_refuses(isolated_home, capsys):
    home = isolated_home
    d = home / "actually-a-dir"
    d.mkdir(parents=True)
    r = db_locate.resolve(str(d), None, None, _default(home))
    with pytest.raises(SystemExit) as ex:
        db_locate.preflight_up(r, home=home, default=_default(home),
                               config_exists=True)
    assert ex.value.code == 3
    assert "DIRECTORY" in capsys.readouterr().err


def test_zero_byte_config_db_refuses_flag_notices(isolated_home, capsys):
    home = isolated_home
    empty = _mkdb(home / "proj" / "agora.db", size=0)
    r_cfg = db_locate.resolve(None, None, str(empty), _default(home))
    with pytest.raises(SystemExit) as ex:
        db_locate.preflight_up(r_cfg, home=home, default=_default(home),
                               config_exists=True)
    assert ex.value.code == 3
    assert "EMPTY (0 bytes)" in capsys.readouterr().err
    # The same file chosen EXPLICITLY this run proceeds with a notice.
    r_flag = db_locate.resolve(str(empty), None, None, _default(home))
    notices = db_locate.preflight_up(r_flag, home=home,
                                     default=_default(home),
                                     config_exists=True)
    assert len(notices) == 1 and "empty" in notices[0]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_unwritable_parent_refuses(isolated_home, capsys):
    home = isolated_home
    parent = home / "locked"
    db = _mkdb(parent / "agora.db")
    parent.chmod(0o500)
    try:
        r = db_locate.resolve(str(db), None, None, _default(home))
        with pytest.raises(SystemExit) as ex:
            db_locate.preflight_up(r, home=home, default=_default(home),
                                   config_exists=True)
        assert ex.value.code == 3
        err = capsys.readouterr().err
        assert str(parent) in err and "-wal" in err
    finally:
        parent.chmod(0o700)


# -- backup / restore share the policy ----------------------------------------

def test_backup_missing_db_gets_moved_project_diagnosis(isolated_home,
                                                        capsys):
    home = isolated_home
    r = db_locate.resolve(None, None, str(home / "gone" / "agora.db"),
                          _default(home))
    with pytest.raises(SystemExit) as ex:
        db_locate.preflight_backup(r, home=home, default=_default(home))
    assert ex.value.code == 3
    assert "moved or renamed" in capsys.readouterr().err


def test_backup_flag_missing_plain_refusal(isolated_home, capsys):
    home = isolated_home
    r = db_locate.resolve(str(home / "nope.db"), None, None, _default(home))
    with pytest.raises(SystemExit) as ex:
        db_locate.preflight_backup(r, home=home, default=_default(home))
    assert ex.value.code == 3
    assert "nothing to back up" in capsys.readouterr().err


def test_restore_missing_parent_refuses_named(isolated_home, capsys):
    home = isolated_home
    target = home / "no-dir" / "agora.db"
    r = db_locate.resolve(str(target), None, None, _default(home))
    with pytest.raises(SystemExit) as ex:
        db_locate.preflight_restore(r, home=home, default=_default(home))
    assert ex.value.code == 3
    assert str(target.parent) in capsys.readouterr().err


def test_restore_existing_parent_proceeds(isolated_home):
    home = isolated_home
    parent = home / "place"
    parent.mkdir(parents=True)
    r = db_locate.resolve(str(parent / "agora.db"), None, None,
                          _default(home))
    db_locate.preflight_restore(r, home=home, default=_default(home))


# -- cmd_up persistence ordering (F4) ------------------------------------------

def _up_args(*extra: str):
    return cli.build_parser().parse_args(["up", *extra])


def test_up_refusal_leaves_config_untouched(isolated_home, capsys):
    home = isolated_home
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.json"
    cfg_path.write_text(json.dumps(
        {"url": "http://127.0.0.1:18999", "admin_key": "k",
         "db_path": str(home / "moved" / "agora.db")}))
    before = cfg_path.read_bytes()
    with pytest.raises(SystemExit) as ex:
        cli.cmd_up(_up_args("--port", "18999"))
    assert ex.value.code == 3
    assert cfg_path.read_bytes() == before


def test_up_success_persists_normalized_path_after_open(
        isolated_home, monkeypatch, tmp_path):
    home = isolated_home
    order: list[str] = []
    import agora.hub.app as hub_app
    import uvicorn
    monkeypatch.setattr(hub_app, "create_app",
                        lambda **kw: order.append("open") or object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: order.append("run"))
    monkeypatch.setattr(cli, "_smoke_check_mcp", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_preflight_port", lambda *a, **k: None)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    monkeypatch.chdir(tmp_path)
    rel = os.path.relpath(chosen / "agora.db")
    cli.cmd_up(_up_args("--port", "18998", "--db", rel))
    cfg = json.loads((home / "config.json").read_text())
    assert cfg["db_path"] == str(chosen / "agora.db")   # normalized absolute
    assert order == ["open", "run"]                     # saved between them


def test_up_failed_open_does_not_persist_config(
        isolated_home, monkeypatch, tmp_path):
    home = isolated_home
    import agora.hub.app as hub_app

    def boom(**kw):
        raise RuntimeError("simulated open failure")

    monkeypatch.setattr(hub_app, "create_app", boom)
    monkeypatch.setattr(cli, "_smoke_check_mcp", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_preflight_port", lambda *a, **k: None)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    with pytest.raises(RuntimeError):
        cli.cmd_up(_up_args("--port", "18997",
                            "--db", str(chosen / "agora.db")))
    assert not (home / "config.json").exists()
