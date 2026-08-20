"""Intra-doc links resolve — the `mkdocs build --strict` gate, in pytest.

The docs site is built with `--strict`, where a link to a missing page is an
ERROR, not a warning. That build runs in its own workflow, on `docs/**` paths
only, and it does not gate the release: a broken link is therefore invisible
until a tag is already pushed and the pipeline is already running.

The break this exists to catch is not exotic. Moving a backlog item between
`proposed/` and `completed/` silently invalidates every RELATIVE link in it
(and every link TO it), because relative links are resolved from the
containing file's directory. That is a routine act — items move whenever work
lands — so the guard belongs where every `ci` run sees it, on every Python
version, in seconds and with no mkdocs dependency.

Scope matches what mkdocs actually checks: markdown links from files inside
`docs/` to paths on disk. Absolute URLs, mailto/anchors, and site-absolute
paths are mkdocs' business, not ours.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
#: The root policy docs render on GitHub, and README.md is also the package
#: long_description shown on PyPI — a relative link that 404s there is the
#: same defect one directory up, and mkdocs never sees these files.
ROOT_PAGES = ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md",
              "CODE_OF_CONDUCT.md", "ACKNOWLEDGEMENTS.md", "AGENTS.md")

#: Inline markdown links: `[text](target)`. Reference-style links and raw HTML
#: hrefs are deliberately out of scope — the docs corpus uses neither, and a
#: half-accurate parser would teach false failures.
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _pages() -> list[Path]:
    root = [ROOT / name for name in ROOT_PAGES]
    return sorted(DOCS.rglob("*.md")) + [p for p in root if p.exists()]


def _is_local(target: str) -> bool:
    """A link mkdocs resolves against the file tree, rather than the web."""
    if not target or target.startswith(("#", "/", "<")):
        return False
    return "://" not in target and not target.startswith("mailto:")


@pytest.mark.parametrize("page", _pages(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_doc_links_resolve(page: Path) -> None:
    broken: list[str] = []
    for target in LINK.findall(page.read_text(encoding="utf-8")):
        if not _is_local(target):
            continue
        # Drop the fragment/query: mkdocs resolves the PATH, and an anchor
        # into a real page is not this test's business.
        path = target.split("#", 1)[0].split("?", 1)[0]
        if not path:
            continue
        if not (page.parent / path).exists():
            broken.append(target)
    assert not broken, (
        f"{page.relative_to(ROOT)} links to missing files: {broken} — "
        "relative links resolve from the linking file's own directory, so a "
        "page that MOVED takes every one of its links with it. "
        "`mkdocs build --strict` fails the docs site on exactly this."
    )
