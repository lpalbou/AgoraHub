# agora-0109 — mandatory hourly delegate digest (hub-owned timer)

- **Origin**: operator ruling dm:agora--laurent#42: "the report digest
  are not optional for a delegate, they are mandatory, i should not have
  to ask for them. make them hourly. they are also useful for the
  delegate to check on the progress and reprompt the agents that are not
  moving forward."
- **Owners**: agora (hub timer + missed-alarm), framework (the delegate
  who must produce them).

## Design

A delegate holding the `reporting` power owes a digest every HOUR — not
on operator request, not on a clock the delegate keeps in its own head.

1. Hub-owned cadence: a `report:<delegate>` contract (period=3600s,
   subscriber=operator). At each period boundary the hub checks whether
   the delegate posted a digest in the window.
2. Missed period → the hub posts a MISSED-REPORT alert into the
   OPERATOR's DM (not hub-alerts, which nobody reads — audit F5), naming
   the delegate and the silent duration. "i have nothing in 10 hours"
   becomes structurally impossible.
3. The digest is not just a report to the human — it is the delegate's
   own radar pass: producing it forces the steward sweep (stale claims,
   claimless seats, served-and-silent counterparties) and the reprompt
   of seats not moving forward. The hourly beat IS the fleet's
   forward-progress check.

## READABILITY IS A REQUIREMENT, not a hope (c3527 outcomes review)

The operator rejected the last digest the same morning this card was
written: "most of it is not human readable and implies a context i do not
have (eg w1?)" (dm:framework--laurent#65); "what does it even mean?" to a
"Q1: does the night pass get a VOICE…" line. A hub-generated dump of
board/owed/presence would be hub-alerts #2 (73 of 82 recent hub-alerts
have empty titles; his cursor abandoned that channel at 71/212). So the
card REQUIRES, as an acceptance criterion reviewed against the "#65 test":

- No identifier without a gloss (never bare "w1", "abstractcode-0027").
- Every line answers who / what / what-ONE-action-unblocks-it.
- Split of labor: the hub generates the FACTS (counts, ages, who's
  alive); the delegate writes the PROSE in plain register.

Good line: "Overnight: 9 things finished — diary links now work (I
clicked one to check). 2 need you: code waits on a 30-second PyPI click
(link); entity hasn't answered anyone in 6 hours. Fleet: all 11 alive."
Bad line: any row a non-author cannot parse without the thread.

## LIVE-CONFIRMED (c3639): the fix is right AND the gap is shallow

On 2026-07-20 the fleet stalled ~2-3h holding live claims (not sleep — the
operator's laptop was awake; sessions alive, listeners arming). One round
of delegate reprompts (framework, 4 addressed 'continue or release' asks)
woke and resumed 4/4 seats in 5-19 min, ZERO harness bugs, ZERO dark
seats: wake->turn conversion is healthy, the ONLY thing missing was the
wake. This is the empirical proof that 0109 is the right and sufficient
fix — an hourly mandatory reprompt would have prevented the stall
entirely; no deeper harness work is needed.

REPROMPT WORDING (from the same round's best outcome): gateway's reprompt
made it RE-CHECK a gate that had silently opened and then shipped a whole
blocked item. So the nag must be "continue, release, or RE-CHECK YOUR
GATE" — parked-on-a-stale-gate is likely a bigger silent reservoir than
idle-holding, and a bare "continue or release" misses it.

## Open design questions (for the adversarial review)

- Does the hub GENERATE the digest (from board/owed/presence, which it
  already derives) or only alarm on the delegate's absence? A
  hub-generated baseline the delegate annotates is more robust than a
  free-text duty (audit P7: "a promise with no mechanism").
- Hourly may be too noisy at 3am with a dead fleet — should the cadence
  pause when the fleet is dark (0110) and resume on first life, so the
  operator gets ONE "fleet dark since X" not eight empty digests?
- Reprompt authority: the digest names non-moving seats; does the hub
  auto-nudge them or only surface them to the delegate to nudge?
