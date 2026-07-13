---
name: workspace-conventions
description: Workspace layout, file conventions, and the durable working-unit registry. Use when starting work, placing files, organizing task scratch notes, or maintaining a workspace index without requiring automated coordination.
---

# Workspace conventions

This workspace mirrors upstream forge group/org paths. Repos are managed
with `meta`; skills load by walking up `.claude/skills` directories.

## Layout

```
<workspace>/
  .claude/skills/        # workspace-wide
  <group>/
    .claude/skills/      # group-level
    <repo>/
      .claude/skills/    # repo-level, only when owned
```

For repos not owned by the user, personal notes live at
`<group>/.claude/skills/<repo>-notes/`.

## Tools and root files

- `.meta`: JSON map of repo paths to clone URLs.
- `.gitignore`: must ignore every repo path from `.meta`.
- `meta git update`: clone missing repos and pull existing ones.
- `meta git status`: show status across repos.
- `meta exec '<cmd>'`: run a command in each repo.
- `bootstrap.sh`: fresh-machine setup.
- `docs/adr/`: workspace ADRs.
- `TODO.md`: cross-repo personal tracker.
- `scratch/`: per-task artifacts.

## Where things go

Skills, notes, and decisions:

- Workspace layout and task coordination -> the canonical `nk` source checkout
  used by the operator, then run that checkout's installer.
- Other reusable personal skills -> `~/personal/noahkagan/skills/skills/`,
  then run that repo's installer.
- Whole workspace -> `<workspace>/.claude/skills/`.
- One group/team -> `<workspace>/<group>/.claude/skills/`.
- One owned repo -> `<workspace>/<group>/<repo>/.claude/skills/`.
- One unowned repo -> `<workspace>/<group>/.claude/skills/<repo>-notes/`.

ADRs:

- General working practice -> engineering-notes.
- This workspace -> `docs/adr/`.
- One repo -> that repo's docs/conventions.

## Working-unit registry

A working unit has:

- Live docs: the current thing being described and its journal (`README.md`
  plus `JOURNAL.md`, or `decision.md` where applicable).
- Mutable index: slug-only list (`TODO.md`, `checklist.md`).
- Artifacts: `scratch/<slug>/`.
- Optional structured task requirements: `scratch/<slug>/task.json`.

Index entries are plain Markdown bullets containing one link to the artifact
directory. They never use task checkbox markers. The containing tracker bucket
is the entry's sole lifecycle state. Detail belongs in the artifact documents,
not the index.

Tracker buckets are lifecycle states in operational scan order:

- `Blocked`: an external impediment or demonstrated failure prevents progress.
- `Authoring`: an author owns the task.
- `Ready`: complete work is available for an author.
- `Done`: validated work has no remaining action.
- `Backlog`: described work is not ready.
- `Cancelled`: intentionally closed without completion.

The principal lifecycle is Backlog → Ready → Authoring → Done. Review and
repair are internal to Authoring. Automated coordination is optional; placement remains useful on
its own.

An unsatisfied task dependency is not a `Blocked` lifecycle state. Keep a fully
specified dependent task in `Ready`; `task.json` dependency eligibility keeps
it from being claimed until every dependency is Done. Use `Backlog` when the
task itself is not ready to author, and reserve `Blocked` for an external
condition or failure that requires action beyond completing another indexed
task.

When auditing tracker state, compare both directions: every top-level
`scratch/<slug>/` task has a `TODO.md` entry and every entry has a task
directory. Task `README.md` and `JOURNAL.md` files contain durable context and
evidence rather than current lifecycle state. A completed investigation or
specification remains complete when it identifies follow-up implementation
work; that work has its own task.

When structured coordination is used, `scratch/<slug>/task.json` contains
`dependencies`, `capabilities`, `resources`, and `repositories`. Dependencies
name indexed tasks; only Done satisfies them. Repositories are canonical
workspace-relative write targets and become the claim allowlist; read-only
inspection is not listed. Missing tasks, duplicates, and cycles are invalid.
Null means unresolved and an empty list or object means explicitly
unconstrained, except a coordinated task requires at least one repository.
Declare the least restrictive requirements that can complete the whole task:
omit `os` when any eligible platform can do so, and request a GPU only when task
verification uses it. Split work into dependent tasks when different phases
require different or multiple platforms. Legacy three-field manifests remain
readable.

Workspace unit:

- `README.md`: workspace doc.
- `TODO.md`: cross-repo index.
- `scratch/<slug>/`: task artifacts.
- `scratch/<slug>/task.json`: optional structured coordination requirements.

ADR unit:

- `decision.md`: ADR.
- `checklist.md`: optional ADR work index.
- `scratch/<slug>/`: ADR task artifacts.

## Scratch task documents

Every task has a live `README.md` and a live, newest-first `JOURNAL.md`. Read
the README before the Journal. Capture only what helps a future human or agent
resume cold.

Lifecycle inputs are consumed into the Journal: `progress.md` checkpoints
Authoring, `resolution.md` resolves Blocked work, and `cancellation.md` explains
why unfinished work intentionally ends.

Task scratch is the durable evidence plane. Keep retained artifacts as named
companion files or evidence directories under `scratch/<slug>/` and cite them
from `JOURNAL.md` or another task companion. Treat `.workspace/`,
`NK_RUN_TEMP`, and tool temporary directories as ephemeral execution state;
never cite them as durable evidence, and move anything worth retaining into
task scratch before routing.

`README.md` owns current meaning:

- Goal, boundaries, acceptance criteria, non-goals, and verification.
- Branches, PR/MR links, upstream tickets, and related or dependent slugs.
- Open questions and their current resolutions.

`JOURNAL.md` owns sequence:

- Durable decisions, implementation, verification, and review evidence. Keep
  current lifecycle in the mutable index.
- Repro steps, test steps, and validation output.
- What was tried and why direction changed.
- External resources created, so they can be cleaned up.

Each Journal begins with exact `# Task Journal`. Entries use exact
`## Entry NNNN` headings and appear newest first. Put human-readable entry
titles under the reserved heading at H3 or in the body; no other H1 or H2
headings are valid.
Their labels and bodies remain live Markdown rather than a schema. Record a
material amendment when evidence changes the meaning of either document;
ordinary editorial corrections need no entry.

A Checkpoint records progress while preserving the claim and keeping the task
in Authoring. Authors write the fixed `progress.md` input and call
`nk task checkpoint`; the command consumes it into a numbered Journal entry.
Authors may commit unmanaged companions beneath their claimed task directory
before Checkpoint. They leave protected documents and transition inputs such as
`progress.md` uncommitted for lifecycle commands; rejection lists the exact
protected patterns and owning command.

Use nested `task-scratch/` only for evidence packages or phases that cannot be
claimed, completed, or routed independently. A unit with its own acceptance
boundary, dependency edge, claimability, or lifecycle state is a top-level
task with its own `README.md`, `JOURNAL.md`, `task.json`, and tracker entry;
never use nested scratch as a private task queue. Default to the parent
`README.md` and `JOURNAL.md`.

## Launching the agent

Launch from the repo/group/workspace actually being worked on so the
right scoped skills load.

## Task slugs

Format:

```text
<YYYY-MM-DD>[-<tracker>-<id>]-<short-kebab-description>
```

Rules:

- Date is always present.
- Use tracker/id when available.
- Use a nonempty lowercase kebab-case description.
- If several trackers apply, pick one primary and put the others in the task
  README.
- If same-day slugs collide, append `a`, `b`, etc.

Tracker prefixes:

| Tracker | Prefix |
|---------|--------|
| Jira | none, because project keys disambiguate |
| GitHub Issues | `gh` |
| Linear | `lin` |

Add rows as needed; no ADR required for new tracker prefixes.
