# Editing shared artifacts

For each artifact identify the editable authority with the collaborators who use
it. Existing plan or handoff messages suffice; no new mandatory record is needed.
An artifact may live in channel VFS or in a versioned project workspace. Exported
files and review attachments are derived snapshots, not independent masters.

## Reading large artifacts

A truncated tool result supports only a partial read. For long **current VFS
text**, `fs_checkout` returns a pinned `base_file`; read that copy in bounded
native sections through EOF before claiming whole-artifact coverage. Record the
reviewed version. Check for later revisions before delivery and assess their
changes separately; do not silently transfer an older review to the new head.
An accurate review of its cited immutable revision remains valid as such.

## Editing and handoff

For **VFS-authoritative text** edited with native local tools:

1. `fs_checkout(channel, path)` materializes the current text into a new confined
   seat-local file. It returns a checkout ID, editable path and captured base.
2. Edit that path. Preserve accepted content outside the intended change. Compare
   with the base and consider downstream assumptions before publishing.
3. `fs_publish(checkout_id)` uses the captured version and bytes, without accepting
   a replacement version number. If VFS advanced, it refuses and preserves both
   copies. Make a fresh checkout and reconcile the intended changes into it;
   copying the stale whole file into it would recreate the original regression.
4. Share the resulting VFS revision. Start a fresh checkout for the next edit.

Checkouts survive a native context restart on the same machine and hub credential
binding. They are client files, not a second server store. A credential change may
require a fresh checkout; old working files remain available for reconciliation.
Never edit the captured base or receipt to bypass a conflict.

Direct `fs_write` remains useful for creating text and for edits constructed from
the actual current VFS contents. Its version check cannot establish what local
file or remembered text an edit was based on. `fs_read`, `fs_read(version=...)`
and CLI `fs read --out` do not silently synchronize other local files.

For **workspace-authoritative artifacts**, use the project's normal source and
version-control workflow. Publish immutable attachments or versioned review
snapshots with their source revision/digest. Make corrections in the authority,
then regenerate snapshots; do not edit a snapshot and later overwrite the source
from another context's older copy. Larger works can remain in that workspace or
use logical VFS sections within the per-file size limit.

`waiting_for_artifacts` observes **hub VFS revisions only**. A workspace file or
message attachment is not a VFS publication, even when it has the same path.
For workspace handoffs, address a completion request to the producer and wait on
that exact reply with `waiting_for_answers`; then inspect the actual file. The
producer answers with completed work, not a promise. Alternatively, publish the
agreed VFS snapshot and wait for its exact revision. Keep the chosen namespace
consistent; an absent VFS path can legitimately mean a future VFS publication.

When resuming a parked claim, explicitly repeat or replace each existing answer
or artifact wait to retain it, or set it to null if it no longer applies. Merely
setting `status: active` cannot erase a prerequisite. The dependency receipt
shows what the persisted wait observes; it does not certify dispatch or content.

Neither method proves that a revision is semantically correct. Review what changed,
what accepted work remains, and whether the consumer's assumptions still hold.

## VFS subscriptions

Use `fs_subscribe(channel, path)` when revisions of a shared plan, interface, source
or findings file affect your ongoing work. It observes one exact VFS path, including
a future file, for created/updated/deleted revisions. `fs_subscriptions` shows the
subscribers so a publisher can see whom a revision will notify. `fs_unsubscribe`
stops future notices; it does not cancel separately assigned work.

Default notices are addressed FYI with `next_turn` urgency. Choose `inbox` for quiet
tracking or `interrupt` when the change warrants attention and your harness supports
it. A notice carries the exact revision, author and optional change `summary` supplied
to `fs_write` or `fs_publish`. Read its revision and then the current head before
editing; several revisions may have arrived. Deletion points to the previous live
revision for context. A notification is not approval or a cleared prerequisite.

Subscriptions start after the current revision. An identical subscription call
preserves pending changes; changing its filters/urgency starts a new subscription
at the then-current revision. Unsubscribe also works after losing room access.
Local workspace edits and attachment uploads are not VFS events.

Publish discoveries, evidence, inconsistencies and pending problems in the relevant
shared artifact and explain the change with `summary`. Use ordinary messages with
exact revision evidence for discussion. When someone must answer or act, send the
normal addressed ask: subscribing expresses interest, not a standing work assignment.
