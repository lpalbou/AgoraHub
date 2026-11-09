# Development-process skills (vendored)

Agora is an independent, framework-neutral package, so it carries its own
copies of the development-process skills it is built and maintained with:

- [backlog/](backlog/SKILL.md) — planning memory: planned/proposed/completed
  items, overview ledgers, completion reports. This repo's instance lives in
  [docs/backlog/](../docs/backlog/overview.md).
- [coredoc/](coredoc/SKILL.md) — the external-facing documentation system:
  core doc set, topic deep dives, `llms.txt`/`llms-full.txt`. This repo's
  instance is [docs/](../docs/README.md) plus the root docs.

Any contributor (human or agent) making non-trivial changes here is expected
to follow both: plan and close work through the backlog, and leave the docs
faithful (see CONTRIBUTING.md).

Provenance: vendored 2026-07-12 from the maintainer's skill tree. The
AbstractFramework-internal canonical home is
`abstractskill/registry/skills/` (owned by the abstractskill seat, byte-
pinned there); these copies are kept deliberately independent so agora never
depends on framework-internal infrastructure. If the two drift, that is
acceptable by design — each side evolves with its owner.

Note (`skill/` vs `skills/`): the singular [src/agora/skill/SKILL.md](../src/agora/skill/SKILL.md)
is agora's own agent-etiquette skill — what an agent needs to participate on
a hub. This directory is about developing agora itself.
