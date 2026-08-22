"""What build is actually running — the question `__version__` cannot answer.

THE INCIDENT THIS EXISTS FOR (2026-08-22, `decision:a-commit-is-not-a-deployment`).
Nine commits to `src/agora` landed between 14:02 and 19:05. The hub the fleet was
talking to had been started from a 14:02 install and served none of them. Three
seats spent the afternoon building against fields — `reply_to_seq`, `reason`,
`pickup` — that were committed, tested green, announced as shipped, and not
running. Two client seats changed their own code on the strength of those
receipts.

It survived an afternoon because **the only identity the hub served was
`__version__`, and that string was byte-identical across all nine commits.**
"Which build is this?" was answerable only by grepping someone's site-packages,
which is how it was eventually found — by accident, an hour after it started
mattering.

So: `version` is the RELEASE, which moves when a human bumps it. `source` and
`built_at` are what move when the CODE moves, and they are what a receipt must
cite. A client comparing two hubs, or a probe asking "is my fix live?", needs
the second pair; it has never had them.

## The one rule this module must not break

**Never guess, and never fall back to something that looks like an answer.**
A wrong sha is worse than `null`: `null` says "this hub cannot tell you" and
sends the reader to grep, while a plausible-but-stale sha ends the investigation
with a wrong conclusion. Every unknown here is served as `None`, and
`docs/protocol.md` says so. That is the same ruling as `reply_to_seq`'s absent
slot and the obligation `reason` enum's null — a field whose absence is a
statement, not a default.

Resolution is attempted ONCE at import and cached: `whoami` is on every
session-start path, and neither a `git` subprocess nor a stat belongs on a hot
call.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Resolved once at first use. `None` = not yet computed; the value itself
#: always has all three keys, with `None` for anything undeterminable.
_CACHED: dict[str, str | None] | None = None

#: Set by a build/release step that knows the sha the wheel was cut from
#: (the installed case, where no `.git` exists). Deliberately an env var and
#: not a generated file: a generated file that goes missing degrades to a
#: stale committed value, which is the failure mode this module exists to
#: prevent.
_SOURCE_ENV = "AGORA_BUILD_SOURCE"


def _git_sha(package_dir: Path) -> str | None:
    """The short sha of the checkout this source lives in, or None.

    None for every case that is not a definite answer: no `.git` (an
    installed wheel), no `git` binary, a git that errors, a timeout, or a
    tree with no commits. A source tree with UNCOMMITTED changes is a real
    case here — an operator running `agora up` from a dirty checkout — so
    the sha is suffixed `+dirty` rather than hidden. It is still true, and
    "which build" is exactly when you want to know the tree was modified.
    """
    repo = package_dir.parent.parent          # src/agora -> src -> repo root
    if not (repo / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=2.0,
        )
        if proc.returncode != 0:
            return None
        sha = proc.stdout.strip()
        if not sha:
            return None
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=repo, capture_output=True, text=True, timeout=2.0,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            sha += "+dirty"
        return sha
    except (OSError, subprocess.SubprocessError):
        return None


def _built_at(package_dir: Path) -> str | None:
    """Mtime of the installed package, ISO-8601 UTC to the second, or None.

    THIS IS THE FIELD THAT WOULD HAVE CAUGHT THE INCIDENT. It needs no git,
    no build step, and no cooperation from whoever cut the wheel — it works
    on the plain `uv tool install` that was actually running. `14:02` beside
    a commit list ending `19:05` is the whole diagnosis, in one line, without
    knowing which fields to look for.

    Mtime of `__init__.py` rather than of the directory: a directory's mtime
    changes when a sibling `__pycache__` is written, which would make a
    freshly-*imported* stale build look freshly *installed* — the exact lie
    this module is here to stop.
    """
    from datetime import datetime, timezone
    try:
        stamp = (package_dir / "__init__.py").stat().st_mtime
    except OSError:
        return None
    return (datetime.fromtimestamp(stamp, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


def build_identity() -> dict[str, str | None]:
    """`{version, source, built_at}` — what this process is actually running.

    - `version`   the release string (`agora.__version__`). Moves when a human
                  bumps it, so two different builds routinely share one.
    - `source`    short git sha (`+dirty` if the tree is modified), from
                  `AGORA_BUILD_SOURCE` if set, else the checkout, else `None`.
    - `built_at`  when this package was written to disk, ISO-8601 UTC, or
                  `None`.

    `None` means THIS HUB CANNOT TELL YOU — never "same as before", never a
    substitute drawn from `version`. A reader that sees `null` knows to go and
    look; a reader handed a stale-but-plausible sha stops looking.
    """
    global _CACHED
    if _CACHED is None:
        from . import __version__
        package_dir = Path(__file__).resolve().parent
        source = (os.environ.get(_SOURCE_ENV) or "").strip() or None
        if source is None:
            source = _git_sha(package_dir)
        _CACHED = {"version": __version__, "source": source,
                   "built_at": _built_at(package_dir)}
    return dict(_CACHED)


def reset_cache() -> None:
    """Drop the memoised identity. For tests only — nothing in a running hub
    should need this, because a process cannot change the build it is."""
    global _CACHED
    _CACHED = None
