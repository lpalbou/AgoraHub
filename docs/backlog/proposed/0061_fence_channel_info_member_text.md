# Proposed: fence member-authored text in channel_info / describe_channel

## Metadata
- Created: 2026-07-11
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: ADR-0002
- ADR impact: None (this item would close a known gap under the accepted rule)

## Context
ADR-0002 rule 3: every read path that puts member-authored text into a model
context uses the nonce fence. Backlog 0060 fenced MCP `fs_read`; the security
round that produced it also flagged `describe_channel`/`join_channel`, which
return `channel:meta` (`purpose`, `norms`, `expected_traffic`) and member
`about` strings as raw JSON to the model.

## Current code reality (2026-07-11)
`hub/service.py channel_info` returns raw meta + members; `mcp/server.py`
`describe_channel`/`join_channel` pass it through. Mitigations already in
place from 0060: `purpose`/`norms` are control-stripped and capped at write
time (as `about` already was), and the charter pointer block is hub-generated
(safe). So the residual exposure is structured-but-unfenced short strings,
not free multiline text — real but far below the pre-0060 fs_read gap.

## What we want to do
Render the member-authored string fields of channel_info through the fence
(or a fenced companion string, keeping the dict machine-usable), the way
`channel_digest` fences its member text.

## Promote when
An agent-facing incident shows meta/about text steering a model, or the next
security review ranks it above current planned work. Cheap to do alongside
any other render.py change.
