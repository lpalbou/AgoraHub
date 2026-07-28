"""Locate and preflight the hub database path — source-aware, refuse-loud.

Born from the a2a->agora rename incident (2026-07-27): the operator renamed
the project directory while a hub was running. The running hub kept writing
through its open file descriptors, but ~/.agora/config.json still remembered
the old absolute db_path. The next `agora up` after a reboot crashed with a
raw sqlite3 "unable to open database file" (parent directory gone) — and the
NEAR-MISS was worse: had the stale parent still existed, sqlite would have
silently created an EMPTY database and the hub would have booted amnesiac,
splitting weeks of multi-agent history in a way nothing would have named.

The rule this module enforces: an EXPLICITLY chosen path may create a new
database; a REMEMBERED path (config.json, $AGORA_DB) may only ever open an
existing one. Remembered state gets no authority to mint history-less hubs.

Decision matrix (adversarial review 2026-07-27, all rows tested):

  db-file state          FLAG        ENV       CONFIG      DEFAULT
  ---------------------  ----------  --------  ----------  -----------------
  regular file, writable proceed     proceed   proceed     proceed
  is a dir / unwritable  refuse      refuse    refuse      refuse
  exists but 0 bytes     notice      refuse    refuse      notice
  missing, parent there  notice      refuse    refuse      proceed (notice
                                                           if config exists)
  missing, no parent     refuse      refuse    refuse      unreachable
                                                           (home() mkdirs)

Sources: FLAG = --db typed this run; ENV = $AGORA_DB (ambient, months old in
a shell profile is the norm, so it is REMEMBERED state, not an explicit
choice); CONFIG = config.json db_path; DEFAULT = <home>/agora.db. A CONFIG
value that resolves to the default path is reclassified DEFAULT, so deleting
the default db deliberately still boots fresh instead of refusing.

All refusals: exit code 3 (parity with the port-squatter refusal), stderr,
`REFUSING to start:` prefix (grep-stable), and config.json is never touched
— the caller persists config only after a successful open.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Source tags, in resolution order.
FLAG = "flag"          # --db typed on this command line
ENV = "env"            # $AGORA_DB from the environment
CONFIG = "config"      # db_path remembered in config.json
DEFAULT = "default"    # <home>/agora.db

# Sources that are REMEMBERED state: they never mint a new database.
_REMEMBERED = (ENV, CONFIG)


@dataclass(frozen=True)
class ResolvedDb:
    """A resolved db path and where it came from."""
    path: str
    source: str


def _refuse(message: str) -> None:
    """Package-style loud refusal: named diagnosis on stderr, exit 3 —
    the same contract as the port-squatter preflight in cli.py."""
    print(message, file=sys.stderr)
    raise SystemExit(3)


def _normalize(raw: str) -> str:
    return os.path.abspath(os.path.expanduser(raw))


def resolve(flag_db: str | None, env_db: str | None, config_db: str | None,
            default: str) -> ResolvedDb:
    """Pick the db path and classify its source. FLAG > ENV > CONFIG >
    DEFAULT. FLAG/ENV are normalized (expanduser + abspath) so a relative
    path never persists with CWD-dependent meaning; CONFIG must already be
    absolute (a relative remembered path is a config error, refused — it
    would silently re-anchor to whatever directory the next start runs
    from). `:memory:` is refused by name: the hub db must be a file
    (Database(":memory:") remains available to tests directly)."""
    for raw, source in ((flag_db, FLAG), (env_db, ENV)):
        if raw:
            if raw == ":memory:":
                _refuse("REFUSING to start: the hub db must be a file; "
                        "':memory:' is for tests (it would persist a db "
                        "that vanishes at every restart).")
            return ResolvedDb(_normalize(raw), source)
    if config_db:
        expanded = os.path.expanduser(config_db)
        if not os.path.isabs(expanded):
            _refuse("REFUSING to start: config.json carries a RELATIVE "
                    f"db_path ({config_db!r}); its meaning would depend on "
                    "the directory the hub starts from. Edit config.json to "
                    "an absolute path, or start once with an explicit "
                    "`agora up --db /absolute/path` (persisted after a "
                    "successful start).")
        # A remembered path that IS the default is the default: deleting
        # the default db to reset must boot fresh, not refuse.
        if os.path.realpath(expanded) == os.path.realpath(default):
            return ResolvedDb(default, DEFAULT)
        return ResolvedDb(expanded, CONFIG)
    return ResolvedDb(default, DEFAULT)


def _describe(path: Path) -> str:
    """One inventory line: existence, size, mtime — never message counts
    (counting would require opening a database another hub may be serving)."""
    try:
        st = path.stat()
    except OSError:
        return f"{path} (absent)"
    from datetime import datetime
    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"{path} ({st.st_size / 1e6:.1f} MB, modified {mtime})"


def _inventory(home: Path, default: str, skip: str) -> str:
    """What actually exists on this machine, for the 2am operator: the
    default-location db (if any, and if it is not the very path that just
    failed) and the newest snapshot in <home>/backups (if any)."""
    lines: list[str] = []
    dflt = Path(default)
    if dflt.exists() and os.path.realpath(default) != os.path.realpath(skip):
        lines.append(f"    default location: {_describe(dflt)}")
    backups = home / "backups"
    try:
        newest = max((f for f in backups.iterdir() if f.is_file()),
                     key=lambda f: f.stat().st_mtime, default=None)
    except OSError:
        newest = None
    if newest is not None:
        lines.append(f"    newest snapshot:  {newest}")
    if not lines:
        lines.append("    (no db at the default location, no snapshots)")
    return "\n".join(lines)


def _refuse_remembered(resolved: ResolvedDb, home: Path, default: str,
                       reason: str) -> None:
    """The incident-class refusal: a remembered path with no (usable)
    database behind it. Names the cause, inventories what exists, gives the
    two explicit remedies — and guarantees nothing was created or changed."""
    where = ("$AGORA_DB (set in your environment)" if resolved.source == ENV
             else str(home / "config.json"))
    unset = ("    unset AGORA_DB                        "
             "# drop the stale environment override\n"
             if resolved.source == ENV else "")
    _refuse(
        f"REFUSING to start: {where} remembers a hub db at\n"
        f"  {resolved.path}\n"
        f"{reason} Likely cause: the project directory was moved or renamed\n"
        "since the last start. Starting a NEW empty db there would silently\n"
        "orphan every message the old hub holds — so nothing was created.\n"
        "  found on this machine:\n"
        f"{_inventory(home, default, skip=resolved.path)}\n"
        "  fix (pick one):\n"
        "    agora up --db /real/path/to/agora.db  "
        "# point at the moved db (persisted after a successful start)\n"
        f"    agora up --db {default}  "
        "# adopt the default location (or start fresh there)\n"
        f"{unset}"
        "  config is rewritten only after a successful start; "
        "nothing was changed.")


def preflight_up(resolved: ResolvedDb, *, home: Path, default: str,
                 config_exists: bool) -> list[str]:
    """Enforce the matrix for `agora up`. Returns notice lines the caller
    should print; refuses (exit 3) on every state where proceeding could
    split or destroy history. Never creates anything itself."""
    p = Path(resolved.path)
    if p.exists():
        if p.is_dir():
            _refuse(f"REFUSING to start: the db path {p} is a DIRECTORY, "
                    "not a database file. Point --db at the agora.db file "
                    "itself (sqlite would fail with an unnamed 'unable to "
                    "open database file' otherwise).")
        if not os.access(p, os.W_OK):
            _refuse(f"REFUSING to start: the db file {p} is not writable "
                    "by this user. Fix its permissions (the hub needs "
                    "read-write).")
        if not os.access(p.parent, os.W_OK):
            _refuse(f"REFUSING to start: the db directory {p.parent} is "
                    "not writable. SQLite must create -wal/-shm sidecar "
                    "files NEXT TO the db; a read-only parent breaks the "
                    "hub even when the db file itself is writable.")
        if p.stat().st_size == 0:
            if resolved.source in _REMEMBERED:
                # The residue of an aborted boot (or a stray touch): a real
                # hub db is never 0 bytes after one successful open.
                _refuse_remembered(
                    resolved, home, default,
                    "and the file there is EMPTY (0 bytes) — not a database.")
            return [f"note: {p} is empty — starting a NEW hub db there."]
        return []
    # The file is missing.
    if resolved.source == DEFAULT:
        # home() creates itself, so the parent always exists. Silent on a
        # true first boot; one loud line when a config file already exists
        # (something ran here before — say that a NEW db is being minted).
        if config_exists:
            return [f"note: creating a NEW EMPTY hub db at {p} "
                    "(no database found there)."]
        return []
    if resolved.source == FLAG:
        if p.parent.is_dir():
            if not os.access(p.parent, os.W_OK):
                _refuse(f"REFUSING to start: cannot create {p.name} in "
                        f"{p.parent} — directory not writable.")
            return [f"note: creating a NEW hub db at {p}."]
        _refuse(f"REFUSING to start: --db points into {p.parent}, which "
                "does not exist. Create it first (mkdir -p) or fix the "
                "path — refusing to invent directories from a possible "
                "typo.")
    # ENV or CONFIG: remembered path, nothing behind it — the incident.
    _refuse_remembered(resolved, home, default,
                       "and no file exists there.")
    return []  # unreachable; keeps type-checkers honest


def preflight_backup(resolved: ResolvedDb, *, home: Path,
                     default: str) -> None:
    """`agora backup` must read an existing database: backing up nothing is
    never intended, so every missing/empty/non-file state refuses — with
    the full moved-project diagnosis when the path was remembered."""
    p = Path(resolved.path)
    if p.is_dir():
        _refuse(f"REFUSING to start: the db path {p} is a directory, not a "
                "database file.")
    if not p.exists() or p.stat().st_size == 0:
        if resolved.source in _REMEMBERED:
            _refuse_remembered(
                resolved, home, default,
                "and there is no database there to back up.")
        _refuse(f"REFUSING to start: no hub database at {p} — nothing to "
                "back up. Pass --db /path/to/agora.db.")


def preflight_restore(resolved: ResolvedDb, *, home: Path,
                      default: str) -> None:
    """`agora restore` writes the db path: the parent must exist (restoring
    to a genuinely new location is an explicit `mkdir` + `--db`), and a
    remembered path with a missing parent gets the moved-project diagnosis
    instead of a raw FileNotFoundError from the copy."""
    p = Path(resolved.path)
    if p.is_dir():
        _refuse(f"REFUSING to start: the db path {p} is a directory, not a "
                "database file.")
    if not p.parent.is_dir():
        if resolved.source in _REMEMBERED:
            _refuse_remembered(
                resolved, home, default,
                f"and its directory {p.parent} does not exist.")
        _refuse(f"REFUSING to start: the directory {p.parent} does not "
                "exist. Create it first (mkdir -p) or pass --db with the "
                "intended location.")
