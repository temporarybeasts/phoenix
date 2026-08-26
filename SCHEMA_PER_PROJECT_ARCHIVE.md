# Schema-per-project isolation — archived

**Archived:** 2026-08-26 · **Tip of this branch:** `09904f234` (Stage 4b-2h) · **Companion docs:** `phoenix-db-isolation-options.md`, `phoenix-db-isolation-addendum.md`, `/Users/adammyers/.claude/plans/i-d-like-a-technical-tender-koala.md` (full stage-by-stage detail — this file is a condensed pointer into it, not a replacement)

## Why this was archived

Architects originally chose **schema-per-project (B+C)** over **RLS + role-switching (A+B, "row-level")** after both were spiked on real Postgres — see `phoenix-db-isolation-options.md`. Schema-per-project was built out fully and is complete through Stage 4b-2h (below). A production-wiring audit partway through (`phoenix-db-isolation-addendum.md`, 2026-08-24) found the real cost of wiring it into the rest of the codebase (~85 call sites across GraphQL/REST/ingest/daemons/MCP) was materially larger than the spike suggested — exactly the tradeoff the original options doc flagged as schema-per-project's weak point ("High" migration/ops cost vs. row-level's "Low", see that doc's Comparison table).

Architects revisited the decision on 2026-08-25/26 and chose row-level isolation instead. Active development now continues on `rbac-fork` from `dc49c4de9` ("DB-isolation spike — RLS + role-switching (B+A)"), the row-level spike that was built alongside schema-per-project but not pursued further at the time. This branch preserves the schema-per-project work exactly as it stood, in case that direction is ever picked back up.

## What's done (all verified against real Postgres, not just design)

| Stage | What | Commit |
|---|---|---|
| 4a | Project-grant schema (`idp_groups`/`user_idp_group_memberships`/`project_grants`), permission catalog, OIDC group sync. **Mechanism-agnostic — see reuse note below.** | `9d0b37168` |
| Spike | Schema-per-project spike (B+C) — per-project schema/role, cross-schema-FK-correct table cloning, engine-level provisioning hook | `f92743ebc` |
| 4b-1 | Compound GlobalIDs (`"<project_id>:<row_id>"`) + project_id plumbing for Trace/Span/ProjectSession | `db84d753c` |
| 4b-2a | Schema-token fix (`PROJECT_SCOPED_SCHEMA_TOKEN`) so `schema_translate_map` only redirects the 9 project-scoped tables, not shared ones | `3a795f458` |
| 4b-2b | Provisioning widened to all 9 tables, ingest-timing fix, boot-time reconciliation, deprovisioning | `04a09fd30` |
| 4b-2c | Standalone data-migration script for existing rows into per-project schemas | `acdce649e` |
| 4b-2d | The cutover itself: feature flag, ingest write-path routing, ~39 read-path dataloaders, annotation create-mutations | `703edc86a`, `71869b53a`, `2eea3de95` |
| 4b-2e | Cross-schema write-path fixups: `transfer_traces` (full id-remap, not id-preservation), retention fan-out, `clear_project`/`delete_trace(s)` | `211e68d37` |
| 4b-2f | Annotation compound GlobalIDs + the 8 blocked PATCH/DELETE mutations | `ad452a510` |
| 4b-2g | `_delete_orphan_sessions` per-project fan-out | `51ba4abbc` |
| 4b-2h | Bulk project-delete schema/role deprovisioning (4 of 5 call sites were leaking orphaned schemas) | `09904f234` |

Nothing here is known to block `PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED` on correctness grounds — every stage was verified against real Postgres (and SQLite for flag-off regression) before being marked done. Full per-stage detail, including every bug found and fixed along the way, is in `i-d-like-a-technical-tender-koala.md`.

## What remains outstanding if this direction is ever resumed

From `i-d-like-a-technical-tender-koala.md`'s "Roadmap outline (Stages 4b-3 onward)":

- **4b-3 — OTel-ID lookup index.** A shared-schema table mapping OTel span/trace/session ID → `project_id`, populated at ingest. Replaces 4b-1's temporary "join to get it for free" for 3 root lookups, and unblocks the ~28 "doesn't know its project" call sites the original audit found, plus REST's annotation `list_*` endpoints and `trace_by_trace_ids.py`.
- **4b-4 — Bulk multi-project annotation payload decision.** Span/trace/session/document annotation endpoints (REST + GraphQL) currently accept payloads spanning multiple projects with no grouping — needs a product decision (reject vs. partition-and-fan-out), not just plumbing.
- **4b-5 — Dataset-examples-from-spans.** Architecturally cross-project by design (aggregates examples from spans across any project a user has touched) — in direct tension with physical per-project isolation. Needs its own design pass, likely denormalizing span data onto the example at creation time.
- **4b-6 — Coverage-invariant test.** Once routing is live, verify a user blocked from a project via the canonical grant check is also blocked via every other path (GraphQL `node()`, REST, dataloaders, MCP SQL); wire into the nightly smoke check.

Two gaps were carried through the whole program, unrelated to which isolation mechanism is chosen:
- **MCP SQL / agent-tool bypass surfaces** — still Stage 4c's problem regardless of mechanism.
- **Read-replica gap** — `DbSessionFactory` only exposes the primary write engine, so schema-scoped reads always hit the primary even when a replica is configured. Accepted tradeoff, not a blocker; a fast-follow.

## Note on Stage 4a reuse

The project-grant data model (`idp_groups`/`project_grants`/`get_readable_project_ids`) is mechanism-agnostic — it answers "who can access which project," not "how is that enforced." It's already present on `rbac-fork` at the new row-level baseline (`dc49c4de9` is a descendant of `9d0b37168`), so row-level work reads from the same grant data without rebuilding it.

## How to resume schema-per-project, if ever

1. `git checkout archive/schema-per-project-b2c` (or branch off it).
2. Rebase onto whatever `main`/`rbac-fork` has moved to since — this branch's Stage 4a commit (`9d0b37168`) is shared history with the row-level line, so the rebase only needs to replay the schema-per-project-specific commits (`f92743ebc` onward).
3. Pick up at Stage 4b-3 (the next unstarted item above) — everything through 4b-2h needs no rework.
