# nk

`nk` coordinates personal task work across available execution environments.

## Language

**Workspace**:
A node-scoped named checkout of a cluster's workspace repository exposed as an
exclusive resource for an agent run. Moving a checkout to another node creates
a different workspace identity.
_Avoid_: slot

**Workspace reference**:
The canonical `workspace@node` address used by commands and claims, such as
`author-1@linux`. `@` is reserved as the separator. Each component is an opaque
nonempty logical name preserved exactly; only delimiter, traversal, control,
and surrounding-whitespace hazards are invalid.
_Avoid_: node and workspace flag pair, SSH target, filesystem path

**Workspace repository**:
The coordinating Git repository that owns the task queue, project registry,
scratch state, and Node and Workspace bootstrap contracts.
_Avoid_: child repository, project repository

**Node bootstrap**:
An optional Workspace-repository contract that idempotently converges the
system and user environment shared by every Workspace on one Node. It owns
state outside Workspace roots and state intentionally reused across them.
_Avoid_: machine provisioning, Workspace bootstrap, project bootstrap

**Workspace bootstrap**:
An optional Workspace-repository contract that idempotently converges one
Workspace's checkout-local repositories, configuration, and tools. It owns no
shared user or system installation.
_Avoid_: Node bootstrap, project bootstrap, Agent run

**Task requirements**:
A task's explicit capability and resource demand. Null requirements are
unresolved; empty requirements are explicitly unconstrained.
_Avoid_: node inventory, prose hint

**Task manifest**:
The structured declaration of a task's dependencies, capabilities, and
resources. It does not contain prose or lifecycle state.
_Avoid_: task README, `deps.json`

**Journal entry**:
One task `JOURNAL.md` account identified by a task-local, monotonically
increasing `Entry NNNN` heading. Entries display newest first, while the stable
number preserves chronological order and a durable link target. Labels and
bodies are live Markdown conventions rather than a validated schema.
_Avoid_: event, log record, review section

**Checkpoint**:
Records progress while preserving the claim and keeping the task state in
Authoring.
_Avoid_: Progress commit, turn result

**Task amendment**:
A material change to a task `README.md` or to the meaning of an earlier Journal
entry. It updates the live document and adds a Journal entry that preserves the
discovery and rationale; editorial corrections need no Task amendment.
_Avoid_: silent rewrite, immutable revision

**Task dependency**:
A declared prerequisite task. Done satisfies the dependency; cancellation
blocks it; every other lifecycle state remains unresolved. Null means the list
is unresolved and an empty list declares no dependencies. Mutation does not
implicitly change queue placement or ownership.
_Avoid_: ordering hint, `deps.json` key

**Ready task**:
A task whose specification is complete and whose task requirements are
resolved, making it available for Scheduler consideration. Dependencies,
structural fit, and current capacity still determine eligibility.
_Avoid_: new task, unclaimed task

**Blocked task**:
A task that cannot proceed because its specification is incomplete or an
external condition requires intervention. An unresolved declared dependency
alone does not move a task to Blocked. Leaving Blocked records a resolution and
requires an explicit next state.
_Avoid_: needs-more-info task, waiting task

**Blocker**:
The current nonempty, free-form `scratch/<slug>/blocker.md`. Blocked requires
this file in published state and every other published queue forbids it. The
file does not prescribe the task's next state. Resolution records the blocker
in `JOURNAL.md` rather than discarding it.
_Avoid_: task state, historical finding

**Authoring task**:
A task claimed by an author, including work that must be resumed.
_Avoid_: active task, running task

**Submission**:
The operation that binds exact candidate commits while retaining the Authoring
claim for internal review, repair, validation, and completion.
_Avoid_: handoff

**Completion**:
The operation that mechanically integrates an approved and validated candidate
set into current targets, removes its author claim, and moves it from
Authoring to Done. Target movement is retried internally; a merge conflict
remains claimed Authoring work for repair.
_Avoid_: integrate, Git publication

**Task validation**:
The accepted evidence-producing gate applied to an exact candidate set after
independent review. It evaluates candidate quality rather than the quality of a
later target composition.
_Avoid_: Merge validation, target validation

**Target validation**:
External evaluation of integrated target quality over an integration window.
It is separate from candidate review, task validation, and completion.
_Avoid_: Task validation, reviewer validation

**Task claim**:
Durable ownership paired atomically with Authoring; no other queue may carry a
claim. Its file contains the owner, claim ID, specification revision, and
repository boundary. The task path identifies the slug and owner is the
canonical `workspace@node` identity, such as `author-1@linux`. Both components
are logical names rather than an SSH target or filesystem path.
_Avoid_: queue placement, workspace reservation

**Requested actions**:
An actionable repair or diagnostic attempt identified during internal review.
The active author repairs it in place. Missing information or external blockers
instead move the task to Blocked.
_Avoid_: requested changes, review failure

**Task coordination**:
The atomic lifecycle operations that claim, route, review, validate, cancel,
and complete tasks across workspaces.
_Avoid_: Git synchronization, ref management

**Queue scan order**:
The fixed `TODO.md` heading order: Blocked, Authoring, Ready, Done, Backlog,
Cancelled. It prioritizes intervention, activity and quiescence,
fan-out opportunities, completed outcomes, planning, and finally cancelled
work; it is not the lifecycle transition order.
_Avoid_: lifecycle order, alphabetical order

**Task entry**:
The exact queue line ``- [`<slug>`](scratch/<slug>/README.md)``. Slug and path
must match, each slug appears once, and the linked README exists; malformed
entries are invalid rather than ignored.
_Avoid_: unchecked Markdown bullet, duplicate task

**Queue insertion**:
Transitions append to their destination queue. Schedulers claim Ready from top
to bottom after resuming existing claims. Manual order in Ready is durable
routing priority.
_Avoid_: universal prepend, unordered queue

**Node**:
A cluster-scoped, stable, addressable execution environment that declares
capabilities and available resources and owns one or more workspaces.
_Avoid_: worker, agent run, process

**Cluster**:
A group of nodes that expose workspaces for one workspace repository through
one inventory and execution boundary. A cluster dispatches agent runs to
selected nodes but does not own routing or placement policy. One workspace
repository belongs to one cluster; a task queue never spans clusters. Its name
is an opaque nonempty value preserved exactly; only path, traversal, control,
and surrounding-whitespace hazards are invalid.
_Avoid_: scheduler, workspace group

**Agent configuration**:
Harness-native configuration supplied by the Node account or Workspace
repository. `nk` launchers add only run inputs such as the prompt, result
schema, output path, and Workspace. Node authentication and personal
preferences remain external prerequisites; shared project settings belong in
the Workspace repository.
_Avoid_: Agent state, Workspace bootstrap, Node capability

**Desired state**:
A cluster declaration marking nodes and workspaces present or absent. Omission
means unmanaged rather than absent.
_Avoid_: observed state, command history

**Capability**:
A registered, non-consumable Node property that constrains eligibility, such as
operating system or architecture. It is a keyed, typed value whose name is valid
only when `nk` owns a corresponding capability proof.
_Avoid_: resource, prerequisite

**Capability proof**:
Evidence from a managed Agent execution environment that a Node's declared
capability or resource is usable by its Workspace.
_Avoid_: host inventory, health check, capability declaration

**Resource**:
Declared allocatable node capacity or an exclusive node-owned instance required
by an agent run, such as a GPU or workspace.
_Avoid_: capability, prerequisite

**GPU resource**:
A GPU that the managed Agent execution environment can enumerate through its
installed driver. Product-specific compute or rendering readiness is a
prerequisite rather than part of this resource.
_Avoid_: physical GPU, Blender-capable GPU, render validation

**Scheduler**:
The component that matches claimable tasks to eligible nodes and resources,
claims them, and coordinates their execution.
_Avoid_: schedule, cluster

**Scheduling**:
The activity of matching work requirements to available nodes and resources.
It filters for eligibility, resumes existing claims, then considers Ready
authors while preserving top-to-bottom priority. An ineligible earlier task
does not block later work.
_Avoid_: schedule, routing contract

**Structural fit**:
Whether any present, uncordoned Workspace for the task could
satisfy its capabilities and total resource demand with all reservations
released. A task without structural fit moves to Blocked; a suitable but busy
or unreachable Workspace is temporary unavailability and does not change queue
placement.
_Avoid_: current availability, health check

**Cordoned Workspace**:
A present Workspace excluded from scheduling after a deterministic local fault
or explicit operator action until it is explicitly uncordoned. Its cordoned
condition is persistent scheduling state rather than Desired state.
_Avoid_: quarantined Workspace, failed Workspace

**Agent run**:
A bounded attempt by a harness to advance one claimed task.
_Avoid_: worker invocation, scheduler run

**Interrupted Agent run**:
An Agent run whose Node-local supervisor PID no longer exists before a terminal
result was recorded. Its task claim and all Workspace evidence remain intact,
its recorded Node resources remain reserved, and its Workspace requires
explicit operator recovery before another Agent run.
_Avoid_: failed task, released claim, inferred route

**Run supervisor**:
A short-lived node-local process that owns one agent run independently of the
controller connection. Controller observations of supervisor state are bounded;
an unresponsive transport yields unknown state rather than an unbounded probe.
_Avoid_: node daemon, scheduler
