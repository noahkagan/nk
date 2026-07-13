---
name: workstream-decoupling
description: Audit and reshape an nk task queue so independent work can progress safely. Use when a fault has blocked unrelated tasks, one platform waits on another platform's qualification, task boundaries hide parallel work, dependency edges serialize more than their true prerequisites, scarce resources are over-reserved, or the operator wants a proactive throughput audit even without a known failure.
---

# Workstream Decoupling

Maximize useful parallel progress without weakening the outcomes that actually
depend on unstable or unavailable behavior.

Use this vocabulary consistently:

- **Workstream decoupling** is the overall outcome.
- **Dependency decoupling** is the graph operation that removes false edges or
  introduces a more accurate intermediate task.
- **Queue fault containment** is the reactive mode that limits a known defect
  to its demonstrated blast radius.

## 1. Establish the maintenance boundary

Read `TODO.md`, every nonterminal task's `README.md`, `JOURNAL.md`, `task.json`,
and candidate/review/merge/validation evidence before changing the graph.
Record the queue revision and current Scheduler state.

When changing a live queue, stop the Scheduler controller so it cannot create
new claims against moving task definitions. Let detached runs continue; do not
interrupt useful work. Preserve their claims and avoid editing an actively
claimed task unless the change is compatible with the exact work already in
flight.

## 2. Audit every pending task

Do not inspect only the task where the fault appeared. For every non-Done task,
derive:

- the smallest user-visible or operational outcome;
- the repositories and platform that own it;
- the minimum implementation prerequisites;
- the validation evidence that is core to the outcome;
- validation that is broad, redundant, or owned elsewhere;
- platform, GPU, host, and other scarce-resource requirements;
- current candidate and review state; and
- every dependency edge and why it exists.

Classify each edge as an outcome prerequisite, implementation prerequisite,
validation prerequisite, merge-order constraint, or accidental serialization.
Do not use dependencies merely because tasks touch related code.

Start capabilities and resources explicitly unconstrained. Add each restriction
only when the task's remaining outcome or required validation cannot complete
without it. Repository location, the platform where related work happened, a
historical route, or the host an author expects to use is not evidence for an
OS, architecture, GPU, canonical-host, or other scheduling requirement.

## 3. Choose the narrowest decoupling move

Prefer, in order:

1. Remove a stale edge or incorrect capability/resource requirement.
2. Route a task to the platform that owns its remaining outcome.
3. Make a task-specific validation exception for one exact known defect.
4. Split a task at a real independently completable boundary.

Good split boundaries include:

- shared or non-render implementation versus platform-native qualification;
- one platform's operation versus another platform's publication;
- non-destructive migration preparation versus destructive authority cutover;
- deterministic artifact assembly versus scarce-resource runtime acceptance;
- evidence collection versus a product fix owned by another team.

Do not split a small implementation from its focused targeted validation merely
to make the queue look parallel. A split must produce a durable outcome another
task can consume without pretending unfinished behavior is complete.

## 4. Contain a known fault

Name the exact failure signature, affected platform and operation boundary, and
retained evidence. Distinguish affected, possibly affected, and unaffected work.

A task-specific exception must:

- match only the established signature;
- retain the failed command, exact inputs, logs, and native evidence;
- keep focused behavior and all unrelated checks mandatory;
- state which result remains unproven; and
- avoid making the fault investigation a dependency of unaffected tasks.

Never turn a flaky pass into evidence of deterministic correctness. Do not
waive a check when rendering, dimensions, publication, or another unstable
operation is itself the task's core outcome.

If a blocker needs deeper diagnosis, use `$deep-blocker-diagnosis`. Do not
create an evidence task merely because root cause is inconvenient.

## 5. Apply the graph change

Use `nk task` lifecycle commands for creation, routing, and state changes. Keep
task intent in `scratch/<slug>/README.md` and dependencies/capabilities/resources
in `task.json`.

When splitting work:

- move requirements rather than duplicate them;
- make the predecessor and successor acceptance criteria non-overlapping;
- preserve exact candidate evidence where its definition remains valid;
- require re-submission when a changed definition invalidates generated
  evidence; and
- keep platform publication and validation independent unless they share a
  true completed prerequisite.

## 6. Verify the whole queue

Before resuming dispatch:

1. Run `nk task check` for every non-Done task.
2. Inspect the complete dependency graph for cycles and false transitive gates.
3. Confirm every pending task has one explicit disposition: independently
   actionable, sequenced after a real prerequisite, able to progress to a named
   acceptance boundary, or genuinely contained by the fault.
4. Confirm resource declarations match the remaining work.
   For every nonempty capability or resource, cite the acceptance criterion or
   required validation that makes it necessary; otherwise remove it.
5. Record a durable audit explaining every retained and changed edge.

Restart the Scheduler controller, then observe reservation, claim publication,
launch, and the first independent platform fanout. A valid graph is not enough;
real dispatch must demonstrate the intended decoupling.
