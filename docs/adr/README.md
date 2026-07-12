# Architecture Decision Records

Durable, cross-task engineering decisions for agoria (import package `agora`).
An ADR here constrains future design, implementation, and docs; a one-off
implementation detail belongs in the [backlog](../backlog/overview.md), not here.

Status legend: **Proposed** (awaiting maintainer sign-off) · **Accepted** (live
policy) · **Superseded** / **Deprecated** (kept for history, points to the
replacement).

## Index

| ADR | Title | Status | For | Linked backlog |
|-----|-------|--------|-----|----------------|
| [0001](0001-federation-topology-and-handles.md) | Federation topology — one central hub; handles are provenance metadata | **Proposed** | Whether agoria is a single meeting-point hub or a federation, and what `name@host` means | `planned/federation/0030`, `0031`; `proposed/federation/0040`–`0042` |
| [0002](0002-instruction-tiers-and-charter-authority.md) | Instruction tiers — operator hub rules, owner channel charters, fenced delivery | **Accepted** | Who may put instruction-bearing text into agent contexts, how it is delivered (pull/edge-triggered, fenced), and what "mandatory" may mean | `completed/0060`; `proposed/0061` |

## Process

- Write an ADR for a decision that outlives one task and constrains future work.
- Reader-first: `Context` then `Decision` come first; metadata after.
- A **Proposed** ADR is a decision to be discussed; the maintainer ratifies it to
  **Accepted**. Do not implement work that depends on a Proposed ADR's rule
  without either that ratification or an explicit note that the work is
  exploratory.
- When a decision is replaced, mark the old ADR **Superseded by ADR-NNNN** rather
  than editing its history away.
- Cite the governing ADR from backlog items and from user-facing recommendations.
