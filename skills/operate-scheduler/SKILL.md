---
name: operate-scheduler
description: Monitor, drain, diagnose, repair, and resume a live nk Scheduler and its detached claims. Use for unattended cluster supervision, repeated Scheduler errors, stalled or failed runs, cordoned Workspaces, drain-safe maintenance, staged Node rollout, or an incremental operational incident journal.
---

# Operate Scheduler

Keep useful work running while restoring cluster invariants. Prefer observation
and containment over speculative mutation.

## Observe

1. Read `nk scheduler status --cluster <cluster>`.
2. Confirm whether the controller process is running separately from detached
   workers.
3. Inspect active or failed runs with `nk scheduler logs <claim-id>` or the
   explicit Workspace form. Use `-f` only when following one run is useful.
4. Inspect native child processes when a quiet log may be capturing a long
   build or test. Treat active CPU, advancing artifacts, or a growing log as
   progress.
5. Read authoritative queue and claim state before inferring a lifecycle
   transition from terminal output.

Do not equate a quiet controller, captured subprocess output, a terminal run
record, and a lost claim. Establish which layer changed.

## Classify and contain

Separate these conditions:

- A long operation with evidence of progress is healthy.
- A transport failure is retryable unless repeated evidence establishes a
  Node fault.
- A dirty, detached, unpushed, diverged, or otherwise unsafe checkout is a
  Workspace fault. Preserve it and cordon the Workspace.
- An invalid queue or task specification is shared state, not a reason to
  cordon every reader.
- A repeated launch, stale claim, or partial transition is a coordination
  invariant failure and requires authoritative state inspection.
- A successful author turn that remains Authoring without a Checkpoint is an
  interrupted routing contract requiring recovery, not a reason to cordon a
  clean Workspace.

Stop the Scheduler controller when continued dispatch can multiply damage or
noise. Let detached workers drain naturally; do not kill them merely to make
maintenance convenient. Preserve claims and candidate refs.

Use `nk scheduler wait --cluster <cluster>` after stopping dispatch. A clean
return proves no Workspace is reserved or running. An observation failure does
not prove a drain.

## Repair threshold

Apply a long-term fix only when all of the following hold:

- The exact failure is reproduced or retained in authoritative evidence.
- The faulty boundary and violated invariant are identified.
- The proposed behavior distinguishes neighboring cases such as transport,
  Workspace, and shared-state failures.
- A narrow regression check can prove the invariant.
- The change preserves active claims and unpublished work.

Otherwise, contain the condition and record it without changing product code.
Do not turn one incident into speculative configuration, recovery protocols,
or a second lifecycle system.

## Roll out

1. Test the narrow repair and the full nk suite.
2. Commit and push nk, then install it on the controller.
3. Keep dispatch stopped while Node revisions differ.
4. When dispatch is stopped and every Workspace is drained, converge the whole
   Cluster with `nk cluster setup --cluster <cluster>`. Use `--node <name>`
   only for an isolated Node repair or a deliberately staged rollout. Converge
   every Node before restarting the Cluster.
5. Exercise the repaired path on the real Node. A unit test or clean setup is
   not evidence for native harness, filesystem, GPU, or network behavior.
6. Remove only artifacts whose ownership and disposability are established.
7. Uncordon only after the original fault no longer reproduces and checkout
   preservation is proven.
8. Restart dispatch and watch reservation, launch, claim publication, and the
   first durable route for regressions.

## Keep an operations journal

Store operational evidence outside the task queue at:

```text
reports/nk/<cluster>/<YYYY-MM-DD>.md
```

Do not add the journal to `TODO.md`, create `task.json`, or give it a queue
state. A journal is chronological cluster evidence, not schedulable work.

Update it incrementally with:

- observation time and current controller/run state;
- symptoms and affected Workspace or claim identities;
- evidence separating root cause from consequence;
- containment performed;
- applied commits, checks, and real rollout probes;
- unresolved incidents and the next safe action.

Correct earlier inferences when stronger evidence arrives. Keep durable facts
and decisions; omit raw polling repetition.
