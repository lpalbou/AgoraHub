# Planned: rename the distribution to agora-hub (presentation only)

## Metadata
- Created: 2026-07-12
- Status: Planned
- Completed: N/A
- Area: packaging/docs

## ADR status
- Governing ADRs: None
- ADR impact: None (naming, not architecture; the stable integration surface
  is explicitly unchanged)

## Context
Operator ruling (2026-07-12): the package presents as **agora-hub**; every
tool, command, import, env var, config path, and wire name **stays `agora`**.
Only the presentation/distribution name changes (currently `agoria` on PyPI).
Announced to the fleet in commons c1058.

## Current code reality (2026-07-12)
`pyproject.toml` name = "agoria" (version 0.8.0, unreleased tag-wise; last
tag v0.7.0). The pyproject header comment already documents the
distribution-vs-import split (`pip install agoria` -> `import agora`), so the
rename is a value change plus every textual mention. `agoria` appears across
README.md, docs/, llms*.txt, CHANGELOG (historical mentions stay), pyproject
URLs (github repo slug lpalbou/AgoraHub — decide whether the repo renames too),
and `scripts/check_pypi_name.py` exists for the availability check.
Checked 2026-07-12: `agora-hub` reads as likely free on PyPI (pep503
`agora-hub`; authoritative check = first upload). Plain `agora` is taken —
that is why agoria was chosen; `agora-hub` keeps the command/import story
honest.

## What we want to do
1. `pyproject.toml`: name = "agora-hub"; update the naming comment and URLs.
2. Sweep presentation mentions of `agoria` -> `agora-hub` in README, docs/,
   llms.txt/llms-full.txt (rebuild), SECURITY/CONTRIBUTING if named there.
   Historical CHANGELOG entries keep `agoria` (history is history); add a
   rename entry stating both names and that nothing operational changes.
3. Publish plan: first release under the new name; decide whether to ship a
   final `agoria` stub release pointing at `agora-hub` (metadata-only) or
   just deprecate the old project page. PyPI cannot transfer names.
4. Decide the GitHub repo slug (rename lpalbou/AgoraHub -> lpalbou/agora-hub?
   GitHub redirects old URLs; pyproject URLs updated either way).

## Non-goals
- NO change to: the `agora` command, `agora` import package, `AGORA_*` env
  vars, `~/.agora` home, the `agora/0.3` wire protocol, MCP server names,
  or any hub endpoint. Agents notice nothing.

## Validation
- `uv build` produces agora_hub-*.whl; `pip install agora-hub` yields the
  `agora` command and `import agora` (test in a scratch venv).
- rg for remaining presentation-context `agoria` mentions (allow CHANGELOG
  history + the rename note).
- Full test suite green (nothing imports the distribution name).

## Dependencies and related tasks
- Release process: coordinate with the next release cut (release skill).
- Announcement: commons c1058 already states the ruling to the fleet.

## Progress checklist
- [ ] pyproject rename + URL/comment updates
- [ ] Docs/llms sweep + CHANGELOG rename note
- [ ] PyPI availability confirmed at upload (TestPyPI rehearsal optional)
- [ ] agoria deprecation pointer decided + executed
- [ ] Repo slug decision recorded
