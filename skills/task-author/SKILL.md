---
name: task-author
description: Advance one already claimed nk task by slug for one turn inside a caller-owned goal loop.
---

# Task author

Advance exactly one already claimed task, supplied as `$task-author <slug>`, for
one agent turn. The surrounding caller reinvokes this skill until the task
reaches a durable route.
Do not claim or select work, poll a queue, or continue to another task.
Every successful turn must end in Done, Blocked, Cancelled, or a Checkpoint.

## The ladder

Climb in order. Do not skip a rung, and stop adding work as soon as the task has
a verified durable route:

1. **Understand:** verify the claim, read the task and repository instructions,
   and trace the affected flow end to end.
2. **Establish reality:** reproduce the failure or prove the current state. Do
   not repair a report you have not grounded in evidence.
3. **Avoid work:** if the task is already satisfied, validate and route it
   without changing code.
4. **Reuse or repair:** prefer the existing owner, helper, platform feature,
   dependency, or neighboring-project pattern. Fix a shared root cause once.
5. **Implement:** make the smallest task-complete change, with one focused
   regression check for non-trivial behavior.
6. **Prove:** validate the complete exact candidate, not merely the latest edit.
7. **Challenge:** run the required independent reviews, verify their findings,
   and repair only demonstrated task-scoped blockers.
8. **Route:** complete the task when clean; otherwise leave a concrete,
   evidence-backed durable route. Never substitute more prose for progress.

## Claim boundary

Use `$workspace-conventions` for layout and `$task-coordination` for lifecycle
safety. Resolve the current workspace root once, then derive the claim from TODO
placement and `scratch/<slug>/claim.json`. Verify that it belongs to this
workspace. If it does not, report that no verifiable durable outcome was
reached; never acquire or replace a claim.

Author and validate directly in the claimed workspace. Child repositories are
the working candidate checkouts; do not create temporary checkouts or
worktrees for ordinary task work.

The claim records the immutable specification commit and allowed repositories.
Use the live README for current state, but compare it with the claimed version
before acting on semantic changes. Do not change a repository absent from the
claim; route the required expansion for an operator decision. Stay within the
supplied task: do not inspect other tasks, wait for their repairs, or infer
shared ownership.

## Author

1. Read TODO placement, `claim.json`, the complete `README.md`, the
   newest-first `JOURNAL.md`, `task.json`, referenced artifacts, and repository
   instructions. Treat `task.json.repositories` as the write boundary.
2. Do not encode a guess when requirements are unclear. Continue with any
   concrete repair or diagnostic attempt in the Workspace. Use `nk task block`
   only when evidence proves the next required action is external, such as an
   operator decision that cannot be derived from project documentation. Name
   that action and evidence in `blocker.md`.
3. Create or resume `candidate/<slug>` from each affected child repository's
   remote default branch. Keep every task change on the candidate, push
   ordinary milestone commits, and include every changed repository in the
   next submission.
4. Implement findings whose disposition is `repair` without narrowing the task
   to the latest review. Do not implement `follow-up` or dismissed findings.
   Use bounded read-only subagents during implementation when independent
   research, diagnosis, neighboring-project inspection, or prior art can
   sharpen the solution. Treat their output as evidence, not candidate approval.
   Verify the implementation and retain useful evidence in task scratch. Put
   disposable files under `NK_RUN_TEMP` or use tools that honor `TMPDIR`,
   `TMP`, or `TEMP`; do not create task scratch in a shared global temporary
   directory. Follow `$workspace-conventions`: move retained evidence into a
   named companion under `scratch/<slug>/` before routing, never `.workspace/`
   or another temporary path. The Scheduler removes claim temporary files
   after a successful durable route.
   Commit retained task companions in the Workspace control repository before
   Checkpoint. Do not commit protected task documents or transition inputs;
   lifecycle commands own those coordination commits and report the exact
   protected patterns on rejection.
5. Before submission, validate the implementation and inspect the complete diff
   against the task for obvious mistakes. Do not run the formal independent
   review before the exact candidate is recorded.
6. Submit exact remote candidates in dependency order without releasing the claim:

   ```text
   nk task submit --workspace <workspace-root> --slug <slug> \
     --repository group/dependency --repository group/consumer
   ```

   Submit records exact refs and SHAs while the task remains Authoring. Spawn
   three fresh-context read-only reviewers against that exact candidate:

   1. Use `$ponytail` to find a simpler correct solution.
   2. Use `$complexity-audit` and `$improve-codebase-architecture` to find
      task-scoped accidental complexity or misplaced ownership.
   3. Compare the solution with industry SOTA and current best practices
      without requesting scope expansion merely for conformity.

   Consolidate and verify findings, repair every blocker, then resubmit. Run
   all three reviews once for the first exact candidate. After a repair, repeat
   only the review whose finding or concern the repair affects; include another
   reviewer only when the repair materially crosses into that reviewer's
   concern or broadens the solution. Narrow mechanical, test-only, and
   documentation-only follow-ups do not restart unrelated reviews. Do not
   expand scope or manufacture work from aesthetic or speculative suggestions.
   When successive verified findings expose different failure modes in one
   newly introduced mechanism, stop patching it. Restate the required
   invariant, then list and evaluate a few materially different design
   decisions before continuing.
   When validation grows mainly around failure modes of the chosen mechanism
   rather than the required invariant, treat that test surface as a design
   cost and reassess the decision before adding another case.
7. After a clean independent pass, record validation with
   `nk task record-validation`, then run `nk task complete`. Stop after the task
   reaches Done, Blocked, or Cancelled. A merge conflict remains claimed
   Authoring work for repair.

Before yielding while the task remains Authoring, write a nonempty Markdown
fragment without H1 or H2 headings to `scratch/<slug>/progress.md`, then run:

```text
nk task checkpoint --workspace <workspace-root> <slug>
```

Commit every task change in the claim's candidate repositories before
Checkpoint. Commit retained companions beneath the claimed task directory, and
remove only unrelated or transient files. Do not push merely to Checkpoint;
the Authoring claim still protects those local commits.

Before Blocked, Cancelled, or any other route that releases the claim, push and
reconcile every retained candidate commit. A release failure remains Authoring
work and must preserve the transition input for retry.

The Checkpoint consumes the fragment into the Journal while preserving the
claim. Submission and validation do not replace this turn-ending route. Never
publish progress automatically after a failed or interrupted turn.

Returned review work retains candidate refs and `candidate.json`. Append
repairs to the active task, rerun affected checks, and submit new
exact heads. Use `nk task cancel <slug>` only for an explicit cancellation
decision, after writing its reason to `scratch/<slug>/cancellation.md`.

Work only on the active claim. Independent reviewers are fresh-context,
read-only subagents inside it; they never own the task or edit candidates.

Deepen a failed attempt only while a different hypothesis or new evidence can
change the result. Do not repeat the same failure unchanged. Record durable
evidence when the investigation changes direction or explains why work stops;
route a task-specific blocker instead of exhausting the harness.
