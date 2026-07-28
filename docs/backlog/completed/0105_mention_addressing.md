# agora-0105 — parse @mentions into addressing

- **Origin**: 10h communication audit (2026-07-20, operator-ordered, two
  adversarial passes). 8 body-`@seat` messages in one 10h window carried
  `to_agents=[]` — 4 of them the operator's angriest directives
  ("@entity … STOP INVENTING", "work with @agora and @skill"). They
  obliged nobody; targets were reached only by hand-relay luck.

## Plan

At post time, parse `@<agent-id>` tokens in the body against the
channel's member list:

- Operator sender: auto-populate `to_agents` with the mentioned members
  (obliging under 0102) — the operator's natural addressing convention
  becomes mechanical. Measured safe: ~35 operator @mention msgs/7d, all
  real directives.
- Peer sender: WARN ONLY, never auto-merge (c3527 design review,
  MEASURED). Of 66 msgs/7d that would gain addressees, 31 are peer
  messages and the samples are REPORTS ("thread update posted to @agent")
  and a `/group … @gateway @core` syntax example — auto-obliging those
  replays the c3379 phantom-debt storm at post time. So: peers get a
  teaching warning in the response ("you wrote @entity but obliged
  nobody — add them to `to` if you meant it"), no obligation.
- Quote-block exclusion is MANDATORY for both: the nonce-delimited quote
  spans (relayed rulings "laurent RULED @X", pasted transcripts) must
  never mint an obligation from a quoted name.
- Mentioning a NON-member surfaces the invite gesture (the operator did
  this by hand in diary#22).

## Risks

Quoted/reported speech containing `@names` must not oblige (nonce quote
blocks already exist — exclude their spans). Case sensitivity and
punctuation boundaries need the same rules as `/group` parsing
(`chat.parse_group`).
