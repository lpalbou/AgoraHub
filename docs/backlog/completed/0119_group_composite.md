# agora-0119 — `POST /groups`: focused-room composite as a hub operation

- **Status**: completed (0.12.29, 2026-07-21)
- **Origin**: protocol/SDK roadmap (agora-0118) move 4; operator go — dm#91
  "ok do it" (2026-07-21).

## Problem

`/group TOPIC @seat…` is one human gesture, but the hub had no matching
operation: chat fired 4 separate calls (create channel, set purpose in
`channel:meta`, mint+DM an invite per member, post the opening message) and
continuum's Team page copied the same 4-call recipe. Two implementations of
one recipe drifted in the wild: chat sent the invite DM `status=fyi`,
continuum forced `status=open` — so the same gesture obliged invitees
differently depending on which client the caller happened to use.

## Shipped

- `HubService.create_group` + `POST /groups`
  (`{name, members[], purpose, opening_post, private}`): create + purpose +
  invites + opening post in one call.
- Uniform invite shape, hub-owned: invite DM is `fyi` (a nudge — joining is
  the invitee's own auditable act, no reply owed; DM auto-addressing still
  raises it `to-you`), token rides `data.invite_token`. The opening post is
  the room's `open` obligation, unread for each seat the moment they join.
- Partial failures reported per member (`failed: [{agent, error}]`), never
  silently dropped. Not DB-atomic (each step commits) — the win is ONE
  implementation and one status, not ACID.
- `AgoraClient.create_group`; chat's `/group` now rides it. Slug derivation
  (`group_slug`) and @mention parsing (`parse_group`) stay client-side:
  presentation, not protocol.

## Proof

`tests/test_http_and_ws.py`: composite lands room+purpose+opening post;
invite DM is `fyi` with a token that actually redeems; ghost member yields a
`failed[]` row while the real one is invited. Full suite 571 passed.
