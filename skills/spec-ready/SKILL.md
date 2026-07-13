---
name: spec-ready
description: Review any nk task specification by slug and decide whether a fresh author can implement and validate it without inventing product intent, regardless of the task's lifecycle state.
---

# Review specification readiness

Review the named task without editing files or changing task state. Return
`Ready` only when a fresh author can determine what to build and how to prove
completion without choosing unstated behavior. The verdict assesses only the
specification and neither depends on nor changes the task's lifecycle state.

Before reading task or repository content, run
`nk task check --workspace <workspace-root> <slug>`. If it fails, stop without
further investigation and return `Not ready` with one `Spec repair` finding
containing its diagnostic.

Read the task's `README.md`, `task.json`, applicable repository instructions,
and linked authoritative context. Check that its outcome and boundaries are
clear; requirements are possible, consistent, and verifiable; reasonable scope
inferences are either required or excluded; acceptance evidence proves the
mission; implementation freedom remains unless a constraint is necessary; and
consequential unknowns are resolved or explicit. Dependencies and unavailable
Node resources affect placement, not specification quality.

Investigate the repository only as needed to answer two questions:

1. **Repository assumptions:** Do load-bearing entities and seams named by the
   task exist and plausibly support the outcome?
2. **Decision continuity:** Could adding, restoring, removing, or replacing a
   consequential mechanism reverse deliberate prior direction recorded in
   project docs, related task history, or relevant Git history?

Keep both checks bounded. Do not design the implementation or audit unrelated
history. Prior implementation is evidence, not policy; block only on durable
evidence of deliberate direction. A reversal is allowed when the specification
explicitly supersedes that direction with a human decision.

Return exactly `Ready` when no blocker remains. Otherwise return `Not ready`,
then only blocking findings in this form:

```text
Not ready
- [Spec repair] <location> — <missing, ambiguous, or unverifiable contract>
- [Human decision] <location> — <intent or tradeoff that cannot be derived safely>
```

Use `Spec repair` for defects that can return to `write-spec`. Use
`Human decision` for missing intent, consequential tradeoffs, or an implicit
decision regression. Always label a possible decision regression
`Human decision`: only a human may choose whether to preserve or explicitly
supersede durable prior direction, even when preserving it appears inferable.
If a load-bearing question cannot be resolved with bounded inspection, report
it instead of guessing. Combine structural and historical evidence for the
same underlying blocker into one finding. Do not add scores, grades,
non-blocking suggestions, an implementation plan, or file edits.
