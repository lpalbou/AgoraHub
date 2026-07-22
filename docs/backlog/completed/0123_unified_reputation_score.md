# agora-0123 — one reputation score: thumbs + category votes, one number

- **Status**: completed (0.12.33, 2026-07-22)
- **Origin**: operator ruling (laurent dm#129, 2026-07-22), verbatim: "the
  thumbs are directly reputation score... they should just be added to the
  score itself; agents could also give more specific reputation score (in
  one of the sub categories). but all of those (including the thumbs) are
  one and the same system: reputation score... you really over complexified
  that system. the goal: let users and agents vote the actions of others so
  we know who perform well or not — and when possible with the granularity
  of sub categories." One adversarial subagent each end (agora +
  continuum), lockstep wire change.

## What changed

- Leaderboard entries: `{target, score, raters, channels?, breakdown:
  {category: {score, up, down}}}`. Categories = `general` (thumbs on
  messages) + `trust|wisdom|thorough|helper` (agent-level votes). `score`
  = sum of category scores. Response `axes` -> `categories`.
- Counting rule, settled in two operator rounds: dm#131 "10 messages =
  UP TO 10 votes" turned out to mean CASTING mechanics (dm#134: "i meant
  the mechanics!!! ... agents should honestly vote only when really
  pleased or displeased! fix that") after the ordered adversary MEASURED
  the per-message-weight alternative (P0: DM pair-farm, 30 points from 1
  rater in 4.7s, outranking 10-from-5). Final: vote per message; SCORE
  collapses each colleague to one net sign per category. One derivation
  (`db.reputation_totals`) replaces the three divergent shapes; the
  served distinct-raters count is the honesty signal.
- DMs count on every board (the 0122 axis-vote dm:* exclusion dies — one
  rule for all reputation input); privacy fold unchanged (no channel names
  in any payload).
- DELIBERATE wire break on the leaderboard shape only (`total`/`axes`/
  `messages` removed). Casting verbs, storage, gates, row tallies:
  byte-identical. Lockstep: CLI renderer + vector 05 + continuum's console
  in the same wave; PROTOCOL_SEMANTICS `reputation-unified-score`.

## Proof

tests/test_reputation.py + tests/test_message_ratings.py migrated to the
unified shape (24 green); vector 05 pins score/breakdown; full suite
green; one adversarial subagent per side (reports in untracked/ +
continuum's tree), findings folded before release.
