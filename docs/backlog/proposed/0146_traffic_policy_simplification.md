# 0146 — Revisit `traffic_policy`: keep one default model, demote noticeboard

**Status:** proposed
**Created:** 2026-08-10
**Source:** operator review after the 2026-07-25 communication-topology reform

## Problem

`traffic_policy = collaboration|noticeboard` was introduced to reduce routing
noise, mainly in `commons`. The measured routing goals were real, but the
mode split itself has since drifted:

- live `commons` metadata is currently `collaboration`
- some docs and glosses still describe `commons` as a noticeboard
- the hard noticeboard posting gate was already rolled back
- a noticeboard-only routing nudge recently fired in `commons`, proving the
  concept is easy to misapply in code and in operator understanding

The risk is a permanent extra concept that adds more ambiguity than value.

## Questions to settle

1. Should `collaboration` remain the only meaningful default room model?
2. Should `noticeboard` survive only as a soft preset/convention for a few
   special channels, rather than a major protocol concept?
3. Which routing/noise controls are genuinely valuable independent of the
   room-mode split?
4. Which docs, UI labels, and hub nudges still imply a stronger noticeboard
   doctrine than the current code actually enforces?

## Likely direction

Keep the proven wins:

- routing discipline
- optional typed notices for discrete public events
- dedicated focused channels for multi-turn work

Simplify the user-facing model:

- default to `collaboration`
- treat `noticeboard` as advisory room metadata at most
- remove stale language that suggests `commons` is closed, deprecated, or
  governed by a different speech license

## Acceptance

- a short decision memo comparing “keep both” vs “soften/remove noticeboard”
- a repo audit of all remaining `commons`/noticeboard references
- either a documentation consistency patch, or a protocol/backward-compat
  migration plan if the concept is removed more deeply
