# Completed: Message attachments (blob store + `attachments[]` on messages)

## Metadata
- Created: 2026-07-15 (operator ask via continuum's Team page, laurent
  dm 21: attach a document/image from the composer; every recipient gets
  the text AND the files; "enroll an adversarial subagent and iterate
  until it's robust". Design question dm:agora--continuum#8)
- Status: Completed
- Completed: 2026-07-15

## ADR status
- Governing ADRs: extends the append-only record (ADR-0003 lineage):
  attachments are immutable, content-addressed, and become part of the
  verifiable transcript through the message's data hash.
- ADR impact: none new; the no-deletion invariant carries over.

## Context
No attachment path exists: messages carry body/title/data only, and the
per-channel fs is a TEXT workspace (str content, 256 KiB cap) — binary
has nowhere to live and nothing links files to a message or its
delivery. The operator wants composer attachments where recipients
(channels AND DMs) receive files with the text.

## Contract (v1 — continuum reacts before build)

**Storage: channel-scoped, content-addressed, immutable.**
- `POST /channels/{c}/attachments` (multipart; membership-gated,
  rate-limited like posts) → `{id, size, content_type, filename}` where
  `id = sha256(bytes)`. Duplicate upload of the same bytes in the same
  channel is idempotent (same id back).
- Bytes live in a `blobs` table in the hub's SQLite (single-file backup
  property preserved); caps: 16 MiB/attachment, 8 attachments/message,
  both configurable (`agora up --max-attachment-mb ...`).
- DMs use the DM channel; same route.

**Messages: refs, validated at post time.**
- `PostMessage.attachments = [{"id": <sha256>, "filename": "..."}]`;
  the hub validates each id exists IN THIS CHANNEL, fills
  size/content_type from the blob row, sanitizes filename like a title,
  and normalizes into `data["attachments"]` — so attachment identity
  rides the ledger hash chain: the transcript commits to the exact
  BYTES (content addressing), verifiable offline.

**Delivery: refs ride the envelope, bytes are fetched.**
- Envelopes gain `attachments: [{id, filename, content_type, size}]` —
  never inline bytes (inbox economy). Fetch:
  `GET /channels/{c}/attachments/{id}` (membership-gated).
- MCP: `read_attachment(channel, id, download_path)`; CLI:
  `agora attachment get|put`. Chat renders name+size with a fetch hint.

**Serve-side safety (the hub must never become an HTML origin).**
- Served with `Content-Disposition: attachment` +
  `X-Content-Type-Options: nosniff`; active/executable content types
  (text/html, image/svg+xml, application/xhtml+xml, anything script-y)
  are stored verbatim but SERVED as application/octet-stream.
  Consumer surfaces (continuum's proxy) do their own render-safety pass
  before displaying inline — their adversary lane, per the split.
- **content_type is CLIENT-DECLARED metadata, stored verbatim, never
  verified against the bytes** (settled with continuum,
  dm:agora--continuum#10-11): the hub's serve hardening does not depend
  on it being true, and consumers MUST sniff magic bytes before
  inline-rendering on the basis of it (continuum sniffs client-side
  before `<img>` render). The fable5 pass may add a hub-side sniff for
  the serve decision if it finds a concrete exploit path; v1 does not
  promise one.

**Retention.** Append-only like messages; no delete verb in v1; channel
archive (0090) takes the blobs with it (operator-readable). Registered
as hub data in the data-home registry story.

## Non-goals
- No global/cross-channel blob namespace (re-attach = re-upload; dedup
  by hash makes it cheap).
- No inline-bytes delivery in envelopes or notify lines.
- No thumbnailing/transcoding — the hub stores and serves bytes.

## Validation (build gate: fable5 adversarial pass per the operator)
- Upload/fetch round-trip byte-exact incl. content types; caps refuse
  teachingly (413); non-member upload/fetch 403; ref to a foreign or
  missing blob 400 at post time.
- Ledger still verifies; attachment ids visible in the transcript.
- Serve headers: disposition/nosniff always; dangerous types
  octet-streamed.
- DM attachment end-to-end; envelope refs present for all recipients.

## Consumer
continuum's composer + recipient rendering (their adversary covers
upload UX, size/type validation, render safety for untrusted
images/PDFs). They react to this contract BEFORE the hub build starts.

## Completion report (2026-07-15)

Built the hub half to the contract continuum accepted (dm#9-12). Shape as
designed, plus three adversarial-pass hardenings.

- **Storage** (`db.py`): `blobs` table, `blob_put` (INSERT OR IGNORE →
  content-addressed idempotency), `blob_meta`, `blob_get`,
  `blob_channel_bytes`.
- **Service** (`service.py`): `attachment_put` / `attachment_get` (gates:
  membership, pause, closed-state, per-file cap, per-channel quota, rate
  limit), `_validate_attachments` (channel-scoped, server-truth
  normalization, no raw-`data` bypass — wired into `_prepare_structured`),
  `safe_serve_content_type` + `ACTIVE_CONTENT_TYPES`.
- **HTTP** (`http_api.py`): streamed upload with a running cap +
  Content-Length pre-check, off-loop via `run_in_threadpool`; hardened
  serve (disposition/nosniff/octet-stream downgrade, RFC 6266 filename).
- **Delivery** (`attention.py`): refs on every envelope, bytes never.
- **Surfaces**: client `attachment_put/get` + `post(attachments=)`; MCP
  `put_attachment`/`read_attachment` + `post_message(attachments=)`; CLI
  `agora attachment put|get` + `agora post --attach`; `agora up`
  `--max-attachment-mb` / `--max-channel-attachment-mb`.

**Adversarial pass** (one subagent, per the operator's instruction). No
header-injection or IDOR found (sanitize_text strips CR/LF/DEL at store
time; serve filename is ASCII-filtered + RFC 5987 percent-encoded;
`safe_serve_content_type` is the only path to a real Content-Type; fetch
is membership-gated and the id is sha256-validated before the DB touch).
Three findings folded:
- **P1 memory-DoS**: `Request.body()` buffered the whole body before the
  cap. Now streamed with a running total + a Content-Length pre-reject —
  memory bounded to cap + one chunk. Test:
  `test_http_upload_streaming_cap_rejects_oversized_body`.
- **P2 event-loop**: the sync hash + locked BLOB write ran inline on an
  `async` handler. Now `run_in_threadpool`, matching every sync write.
- **P2 disk-DoS**: append-only + no aggregate quota let one member fill
  the disk one distinct blob at a time (the class that hit the volume
  today). Added a per-channel byte quota (default 1 GiB, configurable).
  Test: `test_per_channel_storage_quota`.
The first-writer-wins metadata on identical bytes is intended (documented,
and the message ref carries a per-poster filename override), impact
bounded to identical-content display naming.

**Tests**: `tests/test_attachments.py` — 18 cases (content addressing,
byte-exact round trip, caps/quota, membership gates, malformed/unknown
ids, ref validation + no-bypass + limits, envelope-refs-without-inline,
HTTP round trip, active-type downgrade matrix, DM end-to-end, ledger
commits to attachment identity, streaming cap, Content-Length reject).
Suite: 473 passed.

## Follow-ups revealed
- Consumer wiring (continuum): composer attach + recipient render, their
  adversary on upload UX / render safety, on my SHIP receipt.
- Chat surface (`agora chat`) renders attachment refs as name+size with a
  fetch hint — not built here (agent read paths + web UI are the v1
  consumers); file when the human chat surface needs it.
- `agora status` per-channel attachment-bytes usage line (operator
  visibility for the new quota) — small, deferred until asked.
