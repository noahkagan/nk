---
name: deep-blocker-diagnosis
description: Synchronize an nk workspace safely, inventory its current blocked tasks, deepen each diagnosis through hard evidence, and record a durable blocker report before grilling recommendations and task routing with the operator. Use when asked to investigate current blockers, determine why tasks remain blocked, revisit stale blocker claims, exhaust useful diagnosis before deciding whether to modify, create, cancel, or manually repair work, or conditionally route an unresolved external failure into a bounded evidence task after operator grilling.
---

# Deep Blocker Diagnosis

Investigate every in-scope blocker until further safe diagnosis is unlikely to
change the evidence or routing decision. Separate diagnosis from action.

## Guardrails

- Diagnose before recommending and reach shared understanding before acting.
- Do not repair product code, change queue state, edit task dependencies, or
  create, cancel, or route tasks during diagnosis.
- Do not create an investigation task during diagnosis. A bounded evidence task
  is available only after the operator grilling gate below fails to resolve the
  blocker through a direct decision or existing work.
- Preserve dirty work. Never reset, discard, overwrite, or silently rebase it.
- Use disposable roots such as `/tmp` for reproductions or instrumentation that
  must not touch a candidate or maintained source tree.
- Ask before disruptive host actions, long scarce-resource runs, or expanding
  authority beyond the systems the operator placed in scope.

## 1. Synchronize Safely

Find the workspace root from `TODO.md` and `.meta`, then read its `AGENTS.md`.

1. Inspect workspace and child-repository status before synchronization.
2. Fetch the workspace default branch. Fast-forward the queue checkout only
   when doing so preserves all local work; otherwise keep the checkout intact
   and inspect the fetched queue state explicitly.
3. Fetch relevant child repositories without checking out or pulling over
   dirty trees. Record the exact local and remote revisions used as evidence.
4. Treat `TODO.md` queue placement as current lifecycle state. Do not infer it
   from prose in old task artifacts.

Report any synchronization limitation; do not hide it or manufacture a clean
state.

## 2. Inventory the Blockers

Read the current `Blocked` bucket, or the operator's named subset, in queue
order. For every task, resume from durable state first:

1. `TODO.md`
2. `scratch/<slug>/README.md`
3. the newest-first `scratch/<slug>/JOURNAL.md`
4. `blocker.md`, `resolution.md`, `task.json`, claims, candidate, review,
   merge, and validation evidence when present
5. Referenced reports and retained runtime artifacts
6. Repository context, ADRs, source, tests, and history

Distinguish an actual product failure from a stale statement, dependency gate,
environment condition, strategic decision, interrupted attempt, or mistakenly
modeled task.

Treat dependencies as scheduler eligibility, not lifecycle blockers. A task
with unmet `task.json.dependencies` belongs in `Ready` when it has no separate
unresolved blocker; `nk` Scheduler filtering keeps it unclaimable until those
dependencies are `Done`. If a task in `Blocked` is waiting only for work that
now has an explicit task owner and dependency edge, diagnose the original
failure, then recommend `Ready` during grilling. Keep it in `Blocked` only when
an independent unresolved failure, decision, evidence gap, or required manual
action still prevents safe execution after its dependencies complete.

If the current `Blocked` bucket is empty, report the synchronized queue revision
and stop. Do not reinterpret Ready work as blocked, invent blockers, create a
report with speculative work, or enter a grilling session with nothing to
decide.

## 3. Start the Durable Report

Update an existing relevant diagnosis report when one already owns the scope.
Otherwise create:

```text
reports/blocker-diagnosis/<YYYY-MM-DD>.md
```

The report is not a task. Record:

- scope and synchronized revisions;
- current queue snapshot;
- established facts, load-bearing inferences, and `???` unknowns;
- retained evidence paths and exact reproduction commands;
- ranked hypotheses and what would falsify each one;
- results of each diagnostic probe;
- root cause and confidence, or the precise remaining evidence gap;
- why diagnosis stopped; and
- provisional recommendations reserved for the grilling session.

Update the report as evidence changes. Correct earlier overstatements instead
of layering new prose over them.

## 4. Deepen Each Diagnosis

Use the `$diagnose` discipline where a reproducible failure exists. For each
blocker:

1. Reproduce the exact reported symptom, not a nearby failure.
2. Verify retained artifact integrity and provenance before relying on it.
3. Minimize the failing path and compare exact control, contender, known-good,
   warm/cold, or environment variants when they distinguish hypotheses.
4. Generate 3–5 falsifiable hypotheses. State the prediction for each.
5. Run the cheapest safe probe with the highest expected evidence gain. Change
   one variable at a time.
6. Trace lifecycle facts: who or what interrupted a run, exit status, signal,
   timing boundary, host/session state, discarded diagnostics, and whether a
   claimed timeout actually exists.
7. Prefer primary artifacts—logs, result records, source, traces, hashes, and
   fresh reproductions—over task prose or remembered conclusions.
8. Mark hypotheses established, refuted, or still `???`. Do not promote a
   plausible source path into the historical root cause without linking it to
   the observed failure.
9. Continue with a different probe while new evidence can materially change
   the diagnosis. Do not repeat an unchanged failed attempt.

Hard evidence may disprove the blocker without identifying the historical
transient cause. Record both facts precisely.

## 5. Decide When Diagnosis Is Exhausted

Stop diagnosis for a blocker only when one of these is true:

- the root cause is established by reproducible or retained primary evidence;
- the blocker claim is disproved and the unknown historical cause cannot
  affect the next safe route;
- the exact missing evidence or access is named and no safe in-scope probe can
  recover it; or
- every remaining probe has low expected evidence gain and would not
  materially change confidence, recommendation, or routing.

Do not stop because the work is difficult, slow, or uncertain. Do not claim
exhaustion while an untried safe probe could distinguish live hypotheses.
Record the stopping rationale and any evidence that a recurrence must retain.

Diagnosis of the set is complete only when every in-scope blocker satisfies
this test.

## 6. Enter the Grilling Session

After the report is current, invoke `$grill-with-docs`. Discuss one blocker at
a time and wait for operator agreement before moving on.

For each blocker, present:

1. the blocked task and current state;
2. the deepest evidence-backed root cause;
3. remaining `???` uncertainty and whether it matters;
4. the recommended action; and
5. the proposed route: modify an existing task, create a new task, perform a
   manual repair, cancel, or take no action.

State whether operator review is required. Challenge terminology and update
the durable report or domain documentation as decisions crystallize.

When the agreed route assigns the blocking work to another task, add or confirm
the dependency edge and move the dependent task to `Ready` in the same agreed
action unless an independent blocker remains. Do not use `Blocked` as a waiting
room for dependencies. Verify the resulting task state and dependency manifest
with `nk task status` and `task.json`.

## Task Creation Boundary

Creating a new task may be the agreed result of the grilling session. It is
never a proactive diagnosis action.

- During diagnosis, record a possible follow-up only as a provisional
  recommendation in the report.
- During grilling, explain why existing work cannot own it and recommend the
  smallest task boundary.
- Create the task only after the operator explicitly agrees to that route.
- Apply the same agreement gate to task modification, cancellation, dependency
  changes, queue transitions, and manual repair.
- After cancellation is agreed, record its reason in `cancellation.md` before
  invoking `nk task cancel`; cancellation does not require resolving the
  existing blocker.
- If the operator does not agree, do not create a placeholder, speculative
  backlog item, or administrative wrapper.

Finish with agreed actions and routes clearly separated from actions not yet
authorized.

### Conditional evidence task

Do not recommend an evidence task merely because root cause remains unknown.
First use operator grilling to test whether the blocker can be resolved by:

- repairing the existing candidate or environment;
- clarifying the current task's acceptance boundary;
- making a narrow validation decision;
- correcting dependencies, platform placement, or resources;
- routing work to an existing owner; or
- cancelling obsolete work.

Recommend a new evidence task only when all of these hold:

1. Grilling did not produce a safe direct resolution.
2. The unresolved failure is outside the affected product task's demonstrated
   change boundary or likely belongs to another owner.
3. A bounded evidence outcome remains useful without promising a fix or root
   cause.
4. The evidence can improve reproduction, minimization, attribution, or an
   escalation handoff.
5. The operator explicitly agrees to create it.

Define the task so it can complete when it delivers the strongest achievable
evidence package, even if the underlying defect remains unresolved. Require
exact environment and input identity, reproduction commands, attempt counts,
controls, native artifacts, ranked hypotheses, remaining gaps, and a handoff
that the likely owning team can run without consumer-specific history.

Keep product changes, a promised upstream fix, blanket validation waivers, and
unrelated queue dependencies out of its scope. Use `$workstream-decoupling`
afterward when the unresolved fault may still be serializing unaffected work.
