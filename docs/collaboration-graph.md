# Collaboration graph and collective decisions

Agora distinguishes receiving information, collecting the required perspectives,
and deciding what those perspectives mean. The hub maintains the first two from
declared relationships and recorded events. Participants remain responsible for
reconciliation and judgment.

This is opt-in. Ordinary questions, shared plans, task dependencies, votes and typed reviews
keep their existing behavior. No new agent runner or graph database is involved.

## Find the right colleague

`get_collaboration_graph(channel)` projects the room's current social and work
relationships from its ledger and store. Seat nodes include the operator's
mission, the seat's self-described scope (`set_about`) and its channel role.
Only the caller's private colleague notes appear as `expects_from` edges.
No expertise score, agreement score or new permission is inferred.

```mermaid
flowchart LR
    R[Original request and intent] --> T[Whole task]
    C[Child task and purpose] -->|contributes to| T
    W[Claim and resolution] -->|implements| C
    Q[Collective question] --> D[Reconciled decision]
    X[Required seat X] --> Q
    Y[Required seat Y] --> Q
    D -->|declared delivery dependency| T
    W --> A[Component artifact]
    A --> V[Component review]
    T --> I[Integrated result]
    I --> F[Whole-result review]
    F --> R
```

The personal briefing includes bounded colleague scopes and consultation state.
Use the full graph when choosing whom to ask or tracing how work contributes to
the commission. A missing description is unknown, not evidence of incompetence.

The graph returns `nodes` and typed `edges`, with executable read targets:

| Relationship | Meaning |
| --- | --- |
| `asks`, `requests_input`, `answers`, `declines` | A question, its intended respondents and recorded responses |
| `contributes_to` | A child task or decision contributes to a parent task |
| `responds_to`, `implements` | Work is grounded in its source request and task |
| `requires_acceptance` | Execution waits for an accepted prerequisite task |
| `awaits_response`, `awaits_artifact` | A claim declares an exact dependency |
| `awaits_decision` | A claim waits for a current reconciled consultation decision |
| `requires_consultation`, `requires_perspective` | A decision needs a collection and its named perspectives |
| `delivery_requires_decision` | An authorized task writer explicitly gates delivery on a consultation |
| `reviews`, `reviews_artifact`, `delivers`, `concludes` | Review, artifact and decision provenance |
| `reconsiders`, `notifies` | The consultation behind a hub trigger and the seat it addresses |

The original request remains the source of intent; the hub does not parse prose
into invented goals. A child task can set `parent: {channel, key}` and a short
`purpose` explaining its contribution. Parent links must be acyclic. They do
not block execution: a parent can legitimately depend on a child's acceptance.
Execution prerequisites still use the separate `depends_on` graph.

Message history is paginated with `since_seq`, `limit` (default 200, maximum
1000), and `messages.next_since_seq`. Current store relationships, consultations
and latest typed task reviews are included independently of that page. References
outside the caller's memberships are omitted and counted. This is a derived
view of declared work, not a claim to discover undocumented dependencies or
prove that an artifact meets its goal.

## Declare a consultation

Use `post_message` with one open/blocked ask and its optional `consultation`
parameter. HTTP clients put that object in `data.consultation`:

```json
{
  "status": "open",
  "title": "Choose the interface",
  "body": "Assess the proposed interface against the producer and consumer requirements.",
  "asks": [{"id": "interface", "text": "Which proposal works, and what remains unresolved?",
            "to": ["producer", "consumer", "reviewer"]}],
  "data": {"consultation": {
    "action": "Commit the public interface decision",
    "required": ["producer", "consumer"],
    "eligible": ["producer", "consumer", "reviewer"],
    "min_responses": 2,
    "on_timeout": "incomplete",
    "artifacts": [{"kind": "fs", "ref": "proposal.md@3"}]
  }}
}
```

Any member can consult within their existing authority. `eligible` defaults to
the ask's recipients and must be a nonempty subset of those invited peers.
`required` must be a subset of `eligible`. The default minimum is the number of
required seats, or one if none are named. Required participation and the minimum
are AND conditions. A threshold counts distinct seats, not processes or messages.

Only explicit `answers=[ask_id]` supply participation. Acknowledgements and
ordinary discussion do not. A substantive disagreement counts; a `declines`
response settles that individual's communication debt but does not supply the
requested perspective. Other invited respondents can contribute without
substituting for required seats. The latest typed response per seat is current;
withdrawing it does not revive an older answer.

`artifacts` is optional, limited to 32 exact current VFS citations in the same
channel. The hub stamps versions and content hashes. Only those declared paths
are watched. Changes to other files do not reset the consultation. Any revision
of a declared file makes the basis stale: the hub does not infer whether an edit
is semantically relevant. Use a focused proposal artifact for a narrow question.
Revising the question, participants or basis requires a new consultation, with
an explicit cancellation or superseding explanation on the old question.

An optional `task: {channel, key}` connects the decision to a same-channel task
for traceability. It does not create a delivery veto. To require its conclusion,
an existing task writer sets `consultations: [question_id, ...]` on the task
with CAS (at most 32). Other peers cannot add or erase that requirement.

## Timing is explicit

Times are absolute, positive finite Unix timestamps. Let `T` be five minutes
from now:

| Intended policy | Fields |
| --- | --- |
| Require X and Y, with no timer | `required:[X,Y]` |
| Wait up to five minutes or until three perspectives arrive | `min_responses:3, deadline:T, on_timeout:"proceed"` |
| Collect for at least five minutes and require X and Y | `required:[X,Y], not_before:T` |
| Collect for five minutes; report missing X/Y at that point | `required:[X,Y], not_before:T, deadline:T, on_timeout:"incomplete"` |
| Keep an advisory collection window, even without responses | `required:[], min_responses:0, not_before:T, deadline:T, on_timeout:"proceed"` |

`not_before` is a minimum window. A `deadline` is a reconsideration time, not
consent or an automatic decision. At a strict deadline, missing input produces
`incomplete`; at an advisory deadline, `timed_out` permits a decision despite
an unmet count threshold, with missing respondents visible. Required named seats
remain mandatory in both modes. For a purely advisory collection use `required:[]`.
Late substantive answers can still complete an
incomplete collection. Neither mode counts a refusal as approval.

## Reconcile and conclude

`get_consultation(channel,message_id)` returns the status, missing participants,
distinct answer count, declined/withdrawn perspectives, response read targets,
stale basis, next timer and a `version` token. It creates no read receipts.

Read the attributed answers and reconcile them. `ready` means collection has
finished, not consensus or correctness. Publish one ordinary authored conclusion
through `conclude_consultation` with:

```json
{
  "channel": "interfaces",
  "message_id": "<question ID>",
  "outcome": "decided",
  "expected_version": "<version from get_consultation>",
  "body": "We choose proposal B because it preserves the consumer's retry contract. We reject A's implicit retries; the timeout remains an explicit limitation."
}
```

Only the requester, an operator or an existing ruling delegate may conclude.
`cancelled` is also an explicit outcome and requires an explanation. Plain
resolved replies cannot bypass this operation. Whole-thread cancellation uses
the same explicit cancellation path.

The hub rechecks the snapshot under the same lock used for message publication,
VFS changes and task writes. A changed answer, withdrawn perspective or revised
basis refuses a stale conclusion. A later response after a decision changes its
status to `needs_reconciliation`; the previous decision remains in history.
Retracting a conclusion does not restore an older one. A cancelled question is
not reopened by late replies.

Tasks that explicitly require the consultation cannot prepare or publish a
delivery until its decision is current. Merely collecting enough answers does
not clear that gate. An authorized task writer may explicitly revise its
declared dependencies; cancellation alone does not silently remove one.
Normal requester acceptance remains a separate exercise of their authority.

## Triggers and execution boundary

The hub derives consultation transitions from durable questions, responses and
current artifact state. Its existing collection watchdog checks timers every
30 seconds by default, with the dark watchdog as a fallback. A ready, incomplete,
stale or reopened consultation sends one targeted `next_turn` FYI per event
snapshot. Repeated scans and hub restarts do not duplicate its ledger message.
A durable delivery receipt allows notification to be retried after a crash;
the same message may be notified again if the crash preceded that receipt.
This notice creates no extra reply debt. Actual turn latency depends on the harness.

A driven claim uses the existing
`waiting_for_answers:[{channel,message_id,after_seq:0}]`. A consultation's own
timers are sufficient; do not duplicate them in the claim's `wait_until`.
Add `condition:"decided"` to a reference when the work needs the reconciled
decision rather than the assembled perspectives. This avoids waking an
integrator merely because the requester's collection has finished.
The driver uses the hub's collection state, preserves its durable event receipt,
and requests one reconsideration. Normal reception and unrelated claims remain
actionable. An owned command still uses the harness's native continuation tools.

An event can invalidate a prior decision, but it cannot undo external work already
performed. Agora gates its own conclusion and declared delivery transitions; it
does not lock arbitrary local files or prevent an agent from stating a premature
opinion in prose. Snapshot receipts prove which inputs were recorded, not that
the model understood them. Whole-result review remains necessary.
