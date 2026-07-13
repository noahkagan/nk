---
status: accepted
---

# Personal task toolkit

## 2026-07-11 lifecycle amendment

This amendment supersedes every author/reviewer handoff, Draft, Reviewing,
`qa.md`, and review-evidence rule retained in the historical decision text
below. The current lifecycle is `Backlog → Ready → Authoring → Done`, with
Blocked and Cancelled branches. Authoring is the only claimed state.

The superseded design attempted to enforce independent review through distinct
author and reviewer Workspace identities plus explicit Draft and Reviewing
handoffs. In operation, the handoff discarded useful implementation context
and forced the reviewer or returning author to reconstruct it, while the extra
claims and state transitions introduced coordination failures and blocked
otherwise repairable work. This reduced overall productivity without an
observable increase in solution quality. Review independence is therefore
enforced by a fresh-context, read-only reviewer boundary inside the same
Authoring claim, not by Workspace identity or lifecycle ownership.

Across Agent runs, the Scheduler should make a best-effort attempt to resume
the harness session for the same claim when that harness exposes a stable
resume handle. Session continuity is an optimization, never task authority:
the same Workspace, claim, candidate refs, live task documents, and Checkpoints
must remain sufficient for a fresh Agent run to continue safely when a session
cannot be recovered. The Codex adapter stores the discovered session ID in
claim-scoped local runtime state and supplies it to `codex exec resume` on the
next Agent run. Terminal routing removes that local handle; a Checkpoint keeps
it for the next Authoring turn.

Checkpoint is the mandatory durable handoff between Agent runs. A run that
remains Authoring writes `progress.md`; `nk task checkpoint` consumes it into a
numbered Journal entry while preserving the claim, Authoring state, candidate
work, and clean Workspace. Claimed candidate repositories must have no
uncommitted changes; clean local commits may remain unpushed while the claim is
retained. Every retained commit must be pushed and reconciled before the claim
is released or its Workspace becomes available to another task. The next run
reads the live README and newest-first Journal before continuing.
Harness-session resumption may reduce
reconstruction, but it never replaces this Checkpoint contract.

One goal-wrapped author execution retains its claim through implementation,
exact-candidate submission, fresh-context read-only review, repair, validation,
and completion. `$task-author <slug>` specifies one turn; an interactive
`/goal` or the Scheduler repeats that identical invocation against the same
claim until a durable route. Review is a
procedural requirement inside Authoring, not a queue, verdict command, or
persisted artifact. Completion mechanically requires exact candidate evidence,
matching passing validation, satisfied dependencies, and safe publication.
Every successful author turn ends in Done, Blocked, Cancelled, or a Checkpoint.
A Checkpoint consumes `progress.md` into the Journal while preserving both the
Authoring state and claim; submission and validation are not turn routes.

The fixed queue order is Blocked, Authoring, Ready, Done, Backlog, Cancelled.
Workspaces have no role configuration. `nk task submit` retains the claim;
`nk task record-validation` records candidate-bound validation; `nk task
complete` moves Authoring to Done while merge conflicts remain claimed
Authoring work for repair. Only Backlog can move to Ready manually.

The older detailed sections remain as decision history and are non-normative
where they conflict with this amendment.

## Historical decision text

`nk` is one personal product and repository for workspace conventions, task
coordination, and agent scheduling. These responsibilities share one release
lifecycle, repository, installer, command, and set of skills. Domain boundaries
remain modules and glossary terms rather than separately installed products.

The Scheduler owns routing and placement policy. A Cluster exposes one or more
Nodes and their Workspaces for one Git repository through inventory, resource
reservation, and execution operations. A Node declares non-consumable
Capabilities and allocatable Resources and owns one or more Workspaces. Harness
and network availability are global prerequisites rather than routing
dimensions. An Agent run is one bounded harness attempt to advance a claimed
task.

The installed product has one replaceable application home under `~/.nk` and
one preserved directory per named Cluster under `~/.nk/clusters`. Commands
select a Cluster explicitly, except that the sole configured Cluster is an
unambiguous default. No mutable current Cluster or workspace-directory
inference selects execution state.

The CLI's second argument names a domain noun. Scheduler operations therefore
use `nk scheduler`, not `nk schedule`.

Node and Workspace `add` and `remove` commands edit a Cluster's Desired state
without contacting Nodes. `nk cluster setup` compares that declaration with
reality and applies materialization or removal operations.
Presence and absence are explicit, durable desired states; an omitted object is
unmanaged. Setup applies the declared state noninteractively, including safe
deletions. Setup has no interactive confirmation or dry-run mode.

Cluster `add` and `remove` are local Desired-state edits. Add atomically creates
or restores `~/.nk/clusters/<name>/config.json` with one Workspace repository
and no Nodes. Cluster names are opaque nonempty values preserved exactly.
Structural validation rejects path separators, control characters, leading or
trailing whitespace, and the traversal names `.` and `..`; no case,
punctuation, or artificial length style is imposed. Remove marks the Cluster
and all descendants absent. Neither command contacts a repository or Node.
Setup creates runtime state lazily and applies materialization or deletion.

Node add declares a Cluster-scoped stable name, connection target, registered
capabilities, and nonnegative registered resource capacities without contacting
the target. OS (`linux`, `macos`, or `windows`), architecture (`x86_64` or
`aarch64`), and GPU count are the initial registered vocabulary. A capability
is a keyed value whose registered probe owns its accepted type and compares it
for equality. Resources remain separate nonnegative integer capacities. A name
without an `nk`-owned probe is invalid. Present names are unique; restoring an
absent Node may replace those declared properties.

`nk workspace add WORKSPACE@NODE` declares a Node-scoped unique name, absolute
Node-native path, and one author or reviewer Role without contacting the Node.
All commands address an existing Workspace with the same `workspace@node`
reference; `@` is reserved as the separator. Each opaque nonempty component is
preserved exactly. Structural validation rejects path separators, control
characters, leading or trailing whitespace, and `.` or `..`; no case,
punctuation, or artificial length style is imposed. Connection targets and
paths remain platform-native. Paths are unique per Node.
Restoring an absent Workspace may change its path or Role but not its Node;
moving a checkout to another Node removes one Workspace and adds another. Setup
may clone a missing checkout or verify and explicitly adopt an existing
checkout of the Cluster repository into `nk` ownership.

`nk workspace cordon WORKSPACE@NODE` persistently excludes a present Workspace
from scheduling without changing Desired presence. `nk workspace uncordon
WORKSPACE@NODE` explicitly restores eligibility; both commands are idempotent.
Manual cordoning takes no reason argument and records `cordoned by operator`.
Scheduler also cordons a Workspace when reservation preparation proves a
deterministic local checkout fault. Cordons survive Scheduler restart and are
reported by `nk scheduler status` with their fault. Transport and connectivity
failures remain retryable and do not cordon.
Uncordoning only clears the persisted cordon and performs no immediate Node
access or validation. Normal preparation validates the Workspace before its
next reservation and cordons it again if the fault remains.
Removing a Workspace from Desired state deletes its cordon. Re-adding the same
Workspace reference creates an uncordoned Workspace.

Setup and Scheduler use the same exclusive Cluster controller lock. Setup also
refuses while any detached Agent run remains active or interrupted, so runtime
installation, bootstrap, adoption, and deletion occur only after every run is
terminal or explicitly recovered.

After synchronizing one present seed Workspace on each Node, setup runs the
platform-appropriate optional idempotent Node bootstrap contract
(`node-bootstrap.sh` or `node-bootstrap.ps1`) once for that Node. Node bootstrap
converges system and user state shared by all Workspaces on the Node. Setup then
runs the platform-appropriate optional idempotent Workspace bootstrap contract
(`bootstrap.sh` or `bootstrap.ps1`) for every present Workspace after clone or
origin verification. Workspace bootstrap owns checkout-local child
materialization, configuration, and tools. Missing bootstrap contracts are
successful no-ops. Scheduler runs never invoke either bootstrap.

Bootstrap means idempotent convergence rather than check-only diagnosis. Setup
may install or reconfigure the state owned by each contract without prompting;
an unavailable permission or unsupported repair fails that Node. Explicit
Cluster verification observes the converged result without changing it.
Ownership follows state scope: Node bootstrap owns shared or outside-Workspace
state, while Workspace bootstrap owns only state beneath its Workspace root.
Shared commands are installed once by Node bootstrap and consumed, never
reinstalled, by Workspace bootstrap.

`nk cluster setup` runs only from a clean, committed `nk` checkout and records
that commit SHA in every installation. Scheduler startup requires the
controller and every declared Node to report the same installed SHA before it
launches work.

Setup validates all Desired state before side effects, prints an ordered plan,
and preflights each destructive Node operation before deleting on that Node. A
Node-bootstrap failure prevents Workspace bootstrap and capability proof on that
Node; independent Nodes continue. Setup keeps successful changes, records
per-Node and per-Workspace outcomes, and exits nonzero until a later idempotent
run converges the remainder. Node- and Workspace-scoped plan, bootstrap,
verification, and error activity emits structured operational records.
Subprocess output is observed line by line and emitted with its applicable
context rather than formatting itself.

Removing a Node marks it and all owned Workspaces absent. Setup safely removes
managed Workspaces when the Node is reachable, but whole-Node removal succeeds
when an externally retired Node is unreachable. The declared absent state is
sufficient; `nk` does not retain cleanup evidence for unreachable Nodes.
Workspace-only removal still requires connectivity. Node removal never destroys
or reconfigures the underlying machine.

A Node has a stable name and a replaceable connection target. `localhost` uses
direct execution; every other target delegates to the system OpenSSH client.
The Cluster configuration stores no credential material or copied Agent state.
Users provision each Node account, repository access, Git author identity,
harness authentication, and harness configuration before setup. Shared harness
customization belongs in the Workspace repository through each harness's native
project configuration. Authentication and personal preferences remain
user-local. Upgrading `nk` leaves existing user and administrator configuration
unchanged.

Harness launchers provide only run-specific inputs such as the prompt, result
schema, output path, and Workspace. They preserve native harness configuration
and inherited sandbox restrictions. Codex remains the required Scheduler
harness; `nk` does not install a harness or select its model.
The Codex entrypoint preserves inherited environment restrictions, including
`CODEX_SANDBOX_NETWORK_DISABLED`; the Scheduler's launch environment must match
the intended workload.

Removing a Node does not revoke user-provisioned credentials or clean
machine-level identity. Reachable removal still removes managed Workspaces;
unreachable retirement still succeeds from declared absence. Host identity
cleanup remains manual.

One agentless controller operates the Cluster. Localhost commands execute
directly; remote commands use OpenSSH and short-lived `nk` helpers installed by
`cluster setup`. Nodes run no resident `nk` daemon, registration protocol,
heartbeat service, or RPC server.

Each Agent run has a short-lived Node-local supervisor that owns the harness
process group and durable run metadata, log, and result in its Workspace. The
supervisor is independent of controller and SSH lifetime. Scheduler restart
adopts active or completed runs and reconstructs reservations before claiming
new tasks. A Node-local state observation changes a reserved or running record
to interrupted when its supervisor PID no longer exists. This reconciliation
changes only `run.json`; it preserves the task claim, candidate refs, checkout,
harness log, and artifacts.

Nodes and their resource accounting are Cluster-scoped. The supported model
does not share a physical execution target across Clusters. Reusing a target is
an unchecked convenience escape hatch with independent accounting and possible
overcommit; `nk` provides no global Node inventory, cross-Cluster reservation,
or duplicate-target detection.

The Cluster declares only its coordinating Workspace repository. That
repository's `.meta` registry and platform bootstrap contract own child project
materialization; Cluster configuration does not duplicate the project graph.
One Workspace repository belongs to one Cluster, so its task queue, routing,
controller lock, and `workspace@node` claim owners share one boundary.
Coordinating one repository across multiple Clusters is out of scope. Multiple
independent Clusters may coordinate different repositories.

Each Workspace declares a stable name, desired state, absolute Node-local path,
and one Role. Setup clones or verifies the Cluster's Workspace repository, runs
its platform bootstrap contract, and then records `nk` ownership.
The Roles are author and reviewer; Role selects task-claim behavior and the
bundled prompt rather than exposing prompt filenames as configuration.

Node Capabilities and Resource counts are explicit Desired state. Observed
machine properties never rewrite those declarations.

`nk cluster setup` finishes with a capability proof for every declared
capability and resource through every present Workspace's managed Agent
execution environment. A small `nk` registry maps each supported name to its
deterministic observation and comparison rule; unknown names are rejected.
Setup fails unless every present Workspace passes.
GPU capacity counts devices enumerable through the installed driver from that
managed environment. It does not claim CUDA, Blender, or product runtime
readiness; Workspace bootstrap and project validation own those prerequisites.
`nk cluster verify` runs the same proofs without changing Desired state or
materialized Workspaces. The Scheduler uses the declarations established by
successful setup and does not probe, continuously monitor, or depend on
persisted observed state while routing. Verification also checks Node
reachability, global Git, effective user-local Git author name and email, meta,
harness, and network prerequisites, and present
Workspace path, origin, and harness health. It reports all findings and writes
nothing.

Task creation atomically publishes a live `README.md`, an empty live
`JOURNAL.md`, and unresolved structured capability and
resource requirements, then starts in Backlog. `nk task check` reports
readiness gaps, and only
`nk task ready` may move a task to Ready after the dependency list and both
requirement objects are explicitly resolved. Empty values mean unconstrained;
null means unresolved.
Cluster availability is transient and does not determine durable readiness.
The closed requirement vocabulary is exact `os` and `architecture`
capabilities plus a nonnegative integer `gpu` resource count. Unknown keys are
invalid; Workspace and Role remain implicit. Requirements apply to every Role
for the task; role-specific requirements are deferred.
The ready operation accepts Backlog tasks as readiness promotion and Reviewing
tasks as requested actions. Authoring and Draft move only through their
coordination operations; Done and Cancelled are terminal.
`nk task block SLUG` requires a nonempty local `blocker.md`, moves any
nonterminal task to Blocked, atomically publishes that file, and removes a
author or reviewer claim when the source is Authoring or Reviewing. Automated
blocking generates the file.

The principal task lifecycle is `Backlog` to `Ready` to `Authoring` to `Draft`
to `Reviewing` to `Done`, with `Blocked` and `Cancelled` as branch
states. Ready and Draft are claimable waiting states. An author claim moves
Ready to Authoring, and `nk task submit` publishes the candidate, removes the
author claim, and moves Authoring to Draft. A reviewer claim moves Draft to
Reviewing. `nk task complete` safely completes the reviewed task, removes the
reviewer claim, and moves Reviewing to Done atomically. Failure before target
publication leaves task state unchanged. Completion converges after interruption
by treating an exact prepared merge already at its target as published and
continuing with the remaining repositories. Any nonconvergent state reports an
error while preserving Reviewing and its claim for an explicit reviewer route.
Review and validation mechanics remain internal.
Authoring and Reviewing are therefore the only claimed activity queues; their
emptiness makes task quiescence visible directly in `TODO.md`.
Queue placement and claim ownership form one atomic invariant: Authoring has
exactly one valid author claim, Reviewing has exactly one valid reviewer claim,
and no other queue state has a claim. Claim creation or removal publishes in
the same compare-and-swap mutation as its queue transition. Any mismatch is
invalid rather than interpreted as quiescent or available work.
`scratch/<slug>/claim.json` contains exactly `owner` and `claim_id`. Its path
already identifies the task, and Authoring or Reviewing already identifies the
role; duplicating slug or role would create conflicting lifecycle authorities.
Owner uses the canonical logical `workspace@node` identity, such as
`author-1@linux`, rather than an SSH target or filesystem path.
For requested actions, `nk task ready` requires nonempty local `qa.md`, records
it as a newest-first Journal entry, deletes it, atomically removes the
review claim, and moves Reviewing to Ready. A passing approval records the same
input as a review Journal entry before recording exact review evidence.
Existing compare-and-swap publication handles concurrent mutations; the
transitions add no separate authorization mechanism.
Journal entries have stable, task-local `## Entry NNNN` headings. Labels and
bodies remain live Markdown rather than a second evidence schema. Lifecycle
commands never edit `README.md`; skills read it before the newest-first Journal
and structured evidence.
Authors review the complete exact candidate against the original task before
submission and resolve every evidence-backed blocker. Reviewers independently
reassess the complete exact candidate after every return, collect all concrete
blockers before routing it, and approve only when a fresh pass finds none.
Nontrivial work receives explicit correctness, simplification, load-bearing
complexity, and architecture review, including independent read-only review
when concerns can be separated. A blocker identifies a violated requirement or
a checkable regression; aesthetic, speculative, and out-of-scope suggestions
do not force another round. This quality loop uses the existing Ready,
Authoring, Draft, and Reviewing transitions rather than another lifecycle
state or evidence schema.
Blocked is reserved for missing specification or an external blocker rather
than ordinary review findings.
An unresolved declared dependency alone does not move a task to Blocked.

Failures while approving, preparing, or recording validation preserve
Reviewing and its claim. They report the error for the reviewer to assess;
only the reviewer's explicit route determines whether a requested action moves
the task to Ready or missing information or external intervention moves it to
Blocked.

Every published Blocked task has a nonempty, free-form
`scratch/<slug>/blocker.md`; no other published queue may have that file.
Blocking an already Blocked task publishes edits to it. `nk task unblock`
consumes a nonempty local `resolution.md` and requires an explicit Backlog,
Ready, or Draft destination. It validates that Ready has resolved requirements
or Draft has a valid published candidate, then atomically records the
resolution and resolved blocker in one newest-first Journal entry, removes
both working files, and appends the task to its destination. Blocked review
work can therefore
return to Draft when its candidate remains valid or Ready when it requires
repairs. Only a fresh reviewer claim moves Draft to Reviewing.
`nk task cancel` requires a nonempty local `cancellation.md`, records it in the
Journal, moves any nonterminal task to Cancelled, and atomically removes an
active claim. From Blocked, cancellation records the existing blocker beside
the cancellation reason instead of requiring `resolution.md`. Detached,
retry-safe publication preserves the input, claim, state, and clean control
checkout unless the complete transition is published.

`TODO.md` presents queues in operational scan order rather than transition
order: Blocked, Authoring, Reviewing, Ready, Draft, Done, Backlog, Cancelled.
This puts blocker resolution first, makes activity and quiescence immediately
visible, surfaces author and reviewer fan-out next, then shows completed
outcomes, high-level planning work, and cancelled history.
The parser requires exactly these eight level-two headings, once each and in
this order. Missing, duplicate, renamed, additional, or out-of-order queue
headings are invalid rather than silently ignored.
Every task entry must exactly match
``- [`<slug>`](scratch/<slug>/README.md)``, with matching slug and path, one
entry per slug, and an existing linked README. Malformed entries, duplicate
slugs, path mismatches, and missing task notes are hard errors rather than
silently skipped.
Every transition appends to its destination queue except Reviewing to Ready,
which prepends. The Scheduler claims Ready and Draft from top to bottom, so
requested actions receive the next author opportunity and near-terminal tasks
reach terminality with minimum queueing latency. Manual top-to-bottom order
within Ready and Draft is durable routing priority, not incidental display
order.
`nk task reorder SLUG --before PEER` and `--after PEER` change that priority
only when both tasks are already in the same Ready or Draft queue. Reordering
cannot change queue placement and publishes with the same compare-and-swap
semantics as every Task mutation.

Each task manifest owns its dependencies; there is no workspace-wide dependency
file. Resolution derives from TODO lifecycle state: Done is satisfied,
Cancelled is blocked, and every other state is unresolved.
Missing dependencies and cycles are invalid. Completion requires no dependency
cleanup; cancellation routes nonterminal dependents to Blocked and generates
their `blocker.md` files atomically.
`nk task dependency add TASK DEPENDENCY` and
`nk task dependency remove TASK DEPENDENCY` mutate only a nonterminal subject
task's manifest through normal compare-and-swap publication. They reject
missing tasks, self-dependencies, and cycles. Dependency mutation preserves the
task's queue and claim while immediately changing scheduling and completion
eligibility; any resulting route is a separate explicit operation.
`nk task dependency clear TASK` sets the field to the explicit empty list,
including from unresolved null, and succeeds without publishing when already
empty.
Adding an existing dependency or removing an absent dependency succeeds as a
no-op without publishing. Structural errors still fail.
Dependency list order is preserved: add appends, remove deletes the matching
edge, and duplicate manifest entries are invalid. Commands do not sort or
otherwise rewrite the list.
When dependencies is null, add initializes the explicit list with its new edge
and remove is an idempotent no-op that leaves null unresolved. The ready
operation continues to reject null.
The manifest is `scratch/<slug>/task.json` and has exactly `dependencies`,
`capabilities`, and `resources`. All three begin null and must become an
explicit list or object before Ready; it does not duplicate slug, title, prose,
or lifecycle state.

Mutating Task commands own durable publication of lifecycle state and their
fixed required Journal entries, exposing one semantic lifecycle operation to
callers. Ordinary `README.md`, `JOURNAL.md`, and supporting document edits use
ordinary Git before the transition. Git commits, pushes, leases, refs, and
commit identifiers for the
lifecycle mutation remain implementation details and do not appear as required
CLI steps, arguments, or normal output. The Scheduler calls the Task module
directly rather than parsing its CLI.

Every Task mutation reads an expected control-branch SHA and publishes with
compare-and-swap semantics. No operation uses a remote mutex, blind force push,
or last-writer-wins update. A lost comparison leaves newer state intact and is
reported as a semantic concurrency conflict.

The Workspace-local coordination lock blocks concurrent mutations, keeping
serialization mechanical rather than making callers interpret and retry a
contention outcome. The lock has no parallel metadata or lifecycle semantics.

Task validation binds the exact candidate set rather than a prospective merge.
Reviewer claim preparation places affected child repositories at those exact
candidate SHAs. Authors, reviewers, and validation therefore operate directly
in their claimed workspaces without temporary checkouts, worktrees, or
cross-workspace paths.
Completion prepares that unchanged candidate against current targets and
retries whenever a target moves, without repeating candidate validation. A
clean merge is published; a merge conflict records requested actions and
returns the task to Ready, including after partial multi-repository
publication. Already published repositories are durable progress rather than
an attempted cross-repository rollback.

Completion observes each target before publication. A target containing the
candidate is already complete, an unchanged prepared base is pending, and a
moved target restarts preparation. Retry therefore continues safely after
concurrent target movement, a failed push with an observed new target,
uncertain network result, or process interruption without separate integration
state. After every target contains its candidate, completion rebuilds its
terminal task update on the latest control state and retries compare-and-swap
when intervening changes are unrelated to that task. It revalidates queue
placement, claim, dependencies, and candidate-bound evidence before publishing
Done and never overwrites a concurrent change to the same task. Candidate and
temporary merge-ref cleanup is best-effort and does not gate Done.

From Backlog, `nk task ready` accepts local edits only to that task's
`README.md`, `JOURNAL.md`, and manifest, validates them, reconciles unrelated
published queue changes, and atomically publishes the files with the Ready
transition. From Reviewing it
accepts only `qa.md`; ordinary task documentation is already published through
Git. A concurrent change to the same task preserves local edits and reports a
semantic conflict.

Placement reads a candidate without ownership, reserves its Workspace and Node
resources, and only then attempts the exact Git task claim. A lost task-claim
race releases the reservation before retrying. Durable task ownership never
precedes the resources needed to run it.
Claim selection never changes task lifecycle state. Missing or structurally
invalid candidate evidence is an invalid queue rather than an inferred route.
A valid candidate with missing or changed remote refs may be claimed so the
reviewer can diagnose and route it explicitly. A Workspace matching the
candidate's author owner is ineligible for that review rather than a task
blocker.
For each Role, placement first filters tasks and Workspaces by dependency,
capability, and resource eligibility, then applies the task's top-to-bottom
queue order as priority among eligible candidates. An ineligible earlier task
does not cause head-of-line blocking.
Before that filtering, Scheduler compares each Ready or Draft task with all
declared present, uncordoned Workspaces for its Role while treating every
reservation as released. If none could satisfy the required capabilities and
total resources, the task has no structural fit and Scheduler atomically
appends it to Blocked. A generated `blocker.md` records the fit diagnostic,
including excluded cordoned Workspaces that would otherwise fit. Uncordoning
never changes task lifecycle state; the operator explicitly resolves and
unblocks affected tasks.
A matching Workspace that is busy, unreachable, or short of currently free
capacity is temporary unavailability and leaves queue placement unchanged.
Structural fit uses Desired-state declarations and does not depend on cached
verification health.

Each claim owns a Workspace-local temporary directory exposed as `NK_RUN_TEMP`,
`TMPDIR`, `TMP`, and `TEMP`. Node state reconciliation removes the directory
after the worker and its descendants exit, whether the run succeeded, failed,
or was interrupted.
Durable evidence belongs in the Workspace rather than temporary storage.

Exactly one Scheduler process may run per Cluster, enforced by a Cluster-local
process lock. Workspace and GPU reservations therefore remain in memory. After
a crash releases the lock, restart reconstructs reservations by resuming
durable claims assigned to its Workspaces before claiming new tasks.

Every Agent run executes natively on its selected Node. Native Windows Nodes are
supported; their adapter owns PowerShell execution and quoting, Windows paths,
installation and locking, and Job Object supervision. Workspace repositories
provide `bootstrap.ps1` for Windows alongside `bootstrap.sh` for POSIX.

Interrupting `nk scheduler run` stops new claims, releases the Cluster
controller lock, and exits while active Node-local Agent runs continue. A later
Scheduler invocation adopts them before reusing their Workspaces or resources.
Natural draining is the only managed shutdown behavior. `nk` provides no Agent
run stop, kill, force, or timeout operation; exceptional stuck-run recovery is
direct operator action on the Node. An interrupted run is held out of dispatch
and retains its recorded Node resource reservation until `nk scheduler recover
WORKSPACE@NODE` removes only its stale run reservation. The preserved exact
claim may then resume through normal scheduling. Recovery does not release the
claim, infer a task route, repair the Node, or validate retained partial
evidence.
`nk scheduler wait` observes all present Workspaces in parallel and returns
when none remains reserved or running. It works independently of controller
lifetime and mutates no claim. It fails when any Workspace cannot be observed
or has an interrupted run requiring recovery rather than asserting an unproven
drain. Each remote state observation has a 30-second execution deadline; a
timeout terminates its SSH child and is reported as transport failure.

The Scheduler controller terminal announces startup, blocking, reservation,
launch, completion, and errors by default. Verbose output also announces each
polling operation that can block on a Node, Git, or network boundary before it
begins: run-state checks, queue-reader election, queue reads, and idle cycles.
These lines are flushed immediately. Workspace-scoped records are tab-separated
and carry `WORKSPACE@NODE` as structured context rather than preformatted text.

Operational producers emit records with severity, event name, body, and
optional Workspace context. A synchronous terminal handler owns stream
selection, flushing, and presentation. It pads Workspace references to the
longest reference in the Cluster, prepends a compact local `HH:MM:SS` timestamp,
and renders tab-separated fields. Task command results and internal JSON
protocols are command output rather than operational logging and bypass this
handler.

Run-state checks execute concurrently across Nodes. Each Node returns the state
of all its Workspaces through one bounded command and, when remote, one SSH
connection. When capacity is free, one free Workspace fetches and synchronizes
the queue, then produces a Cluster-wide snapshot using local file reads. A
local Workspace is preferred; failures fall back through free Workspaces in
declared order. Each election is announced before the queue read. A poll that
launches no task attributes its idle event to the successful queue reader; it
is Cluster-scoped when no reader succeeds. The snapshot is advisory.
Routing, reservation, exact claims, and launches remain serial, and the
selected Workspace fetches and synchronizes its root repository and prepares
every child checkout before reserving resources. Failed preparation writes no
reservation, starts no supervisor or Agent, creates no claim, and leaves queue
placement unchanged.
A clean candidate branch belonging to the retained Authoring claim remains in
place across Agent runs even when its local HEAD is ahead of the remote. A clean
non-default branch without a matching claim is returned to the remote default
branch only when its exact HEAD is present on the same-named remote branch and
the default branch can fast-forward. Preparation validates every child before
switching any of them. Dirty, unrecognized detached, orphaned unpushed, or
diverged checkouts remain untouched and the Workspace is cordoned. An exact
detached candidate belonging to durable candidate evidence is reverified and
retained so missing or moved candidate refs remain diagnosable. Transient
transport failures back off without cordoning. The worker repeats child
preparation after reservation to close the
preparation-to-claim race, then fetches, synchronizes, revalidates, and
publishes its claim before its author or reviewer harness starts. A launched
run that records `failed` does not become eligible for retry until 30 seconds
after that terminal record; observing the same run again does not extend the
deadline. An interrupted run does not retry automatically.

`nk scheduler status` reads one Cluster-wide current-activity snapshot,
independent of controller lifetime. It correlates authoritative claims from the
workspace repository's fetched default-branch tree with each present
Workspace's latest Node-local run state. A terminal run remains visible when
it matches the Workspace's current claim; unclaimed completed, failed, or
interrupted records render as idle without changing retained state or logs.
Reserved and running states remain visible during the pre-claim launch window.
An interrupted run matching the current claim remains visible with `recovery
required` scheduling state; no manual cordon is needed to prevent dispatch.
It reports unreachable Workspaces without hiding reachable ones and exits
nonzero when any Workspace or the authoritative claim snapshot cannot be read.
It reads claims only through an idle Workspace and serializes its Git access
with Scheduler snapshot access through the existing per-Workspace task
coordination lock. Each run retains its globally unique task-claim ID after
completion or failure, and current status exposes that ID as the stable log
handle.

`nk scheduler logs CLAIM_ID` resolves that ID across configured present
Clusters and reads the matching Node-local harness log directly or through
OpenSSH, independent of controller lifetime. `--cluster` narrows the search;
the existing `--workspace WORKSPACE@NODE` form remains available. The command
prints the latest 50 lines by default; `--tail N` changes the initial count and
`-f` follows appended output.
