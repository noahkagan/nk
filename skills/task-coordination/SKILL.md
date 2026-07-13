---
name: task-coordination
description: Coordinate automated work on Git-backed workspace task queues with nk task, author claims, candidate refs, validation, and completion.
---

# Task coordination

Use `$workspace-conventions` for layout. `TODO.md` is authoritative and has
Blocked, Authoring, Ready, Done, Backlog, and Cancelled in that order. Only
Authoring carries `scratch/<slug>/claim.json`; Ready is claimable work.

Claim with `nk task claim --workspace <root> [--slug <slug>]`.
`CLAIMED` and `RESUMED` authorize work; `EMPTY` is healthy. One goal-wrapped
author execution retains the claim through implementation, exact-candidate
submission, fresh-context read-only review, repair, validation, and completion.
Review is an internal author workflow, not task state or durable CLI evidence.

Public lifecycle commands:

- `checkpoint <slug> [--resume-after <timestamp>]` consumes nonempty
  `progress.md` into a Checkpoint Journal entry while preserving the Authoring
  claim and state. Use a timezone-qualified ISO 8601 timestamp to defer the
  Scheduler when the next useful evidence cannot exist before that time.
  Authors may commit unmanaged companions beneath the claimed task directory;
  Checkpoint reconciles and publishes those commits while protected documents
  and command-consumed inputs remain lifecycle-owned.
- `submit --slug <slug> --repository <path> ...` binds exact candidate commits
  and removes stale merge/validation evidence without releasing Authoring.
- `record-validation --slug <slug> --task-plan-records <path> --verdict <value>`
  records generic candidate-bound validation.
- `complete --slug <slug>` publishes the validated candidate and moves
  Authoring to Done. Target movement retries internally; merge conflicts remain
  claimed Authoring work for repair.
- `ready <slug>` moves Backlog to Ready. Authors cannot return claimed work to
  Ready.
- `block <slug>` requires `blocker.md`; `unblock <slug> --to Backlog|Ready`
  requires `resolution.md`; `cancel <slug>` requires `cancellation.md` and
  records why unfinished work intentionally ended.
- `follow-up <source> <YYYY-MM-DD-follow-up-description>` consumes
  `follow-up.md` and creates a Backlog task.

Generated evidence is `candidate.json`, `merge.json`, and `validation.json`.
Keep implementation on `candidate/<slug>`. Completion binds exact candidate
SHAs, validation, dependencies, targets, and prepared merges. Do not invent a
review identity artifact: fresh-context review is required by `$task-author`
and orchestrated by the surrounding goal loop.

Let `nk task` own coordination commits, leases, and synchronization. Work only
in the claimed workspace and repository allowlist. Route Blocked only for a
proven external condition; known repairs stay inside the author goal loop.

Every successful author turn ends in Done, Blocked, Cancelled, or a Checkpoint.
Submission and validation remain Authoring milestones and do not satisfy that
routing requirement. Tooling never invents or automatically publishes agent
progress.

Do not spend repeated Agent runs long-polling an unchanged external condition.
This applies to any remote job, service state, approval, resource, timer, or
other observation whose next useful evidence cannot exist yet. Record its
stable identity and expected next evidence in `progress.md`, then Checkpoint
with `--resume-after` based on the condition's demonstrated latency.
The task remains claimed in Authoring, its Workspace remains occupied, and the
Scheduler will not resume it before that timestamp. Prefer a native blocking
watcher only when the expected wait is short enough to finish economically in
the current turn. On a later unchanged observation, increase the delay rather
than checkpointing at a fixed rapid polling interval. Omit `--resume-after` as
soon as concrete work can continue; the next Checkpoint clears any prior
deferral.
