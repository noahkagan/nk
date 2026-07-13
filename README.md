# nk

`nk` is a lightweight personal toolkit for repeatable agentic engineering. It
adds structure around an existing coding harness without replacing it. The same
workflow can support one project or coordinate efforts that span repositories,
trackers, machines, and native resources.

## Lightweight by design

Start with only the layers your work needs:

- **Context skills:** Progressively teach the harness about a project as you
  develop with it. They can be used without task coordination or scheduling.
- **Structured specifications:** Use writing and readiness-check skills to turn
  intent into an implementable, verifiable contract before work is claimed.
- **Structured tasks:** Add explicit lifecycle state, exact claims, isolated
  candidates, independent review, and validation.
- **Clusters and scheduling:** Add automatic routing across declared
  Workspaces, Nodes, capabilities, and resources. A local Node is enough to
  start.
- **Decentralized:** There is no central service, resident node daemon, or
  database. Each Node owns its Agent runs, logs, resources, and runtime state
  independently of the controller connection.
- **Harness agnostic:** Works with an existing coding harness without replacing
  it. The harness retains its own model selection, authentication, and personal
  preferences.
- **Debuggable:** Maps Agent runs, logs, and best-effort resumable harness
  sessions to stable claims, so work can continue in context after a blocker is
  resolved.
- **Self-improving:** Uses the same agentic workflow it coordinates to observe,
  (un)cordon, repair, roll out, and verify `nk` itself.
- **Repeatable across projects:** Gives each project a common execution model
  while it retains its domain knowledge, checks, task state, and contracts.
- **Multi-tracker support by design:** Groups tasks from different trackers in
  one Workspace repository and coordinates changes across them.

## Structured and verified

`nk` is strongly aligned with the structured, verified end of the spectrum in
Google's Kaggle whitepaper,
[The New SDLC With Vibe Coding](https://www.kaggle.com/whitepaper-the-new-SDLC-with-vibe-coding).
That end of the spectrum turns human intent into explicit specifications and
systematically verifies the resulting work.

`nk` applies that discipline through:

- **Context** captured in repository instructions, project documents, and
  skills for individual tasks.
- **Specifications** that record goals, acceptance criteria, non-goals, and
  dependencies before work becomes Ready.
- **Bounded execution** where one Agent run advances one exact claim.
- **Isolated candidates** submitted at exact remote commits rather than moving
  branch tips.
- **Independent review** by fresh-context read-only agents inside the claim.
- **Native validation** on a Node that satisfies the task's declared needs.
- **Durable evidence** for claims, candidates, findings, and validation results.

This rigor cannot make an underspecified task correct. Projects still own their
instructions and validation definitions, and the operator still owns
specification quality before a task becomes Ready.

## Writing better specifications

Use `$write-spec` to build a hardened specification and direct larger efforts
toward independently valuable splits when that reduces risk. Then use
`$spec-ready <slug>` to verify that a fresh author can implement and validate it
without inventing intent before running `nk task ready <slug>`.

## Task workflow

```text
Ready → Authoring ─────────────────────────────→ Done
          │
          ├─ checkpoint ────────────────→ Authoring
          ├─ cancellation.md ───────────→ Cancelled
          │
          ├─ implement → validate → submit exact candidate
          │                              │
          │                    independent review
          │                         │          │
          └──── repair ← verified findings     └─ approve → complete
```

1. Claim it with `nk task claim --slug <slug>`.
2. Invoke `$task-author <slug>` once per Agent run—directly, under `/goal`, or
   through the Scheduler—and repeat as desired while it remains Authoring.
3. Each turn routes through `nk task checkpoint <slug>` (optionally with
   `--resume-after`), `nk task block <slug>`, `nk task cancel <slug>`, or
   `nk task complete --slug <slug>`.
4. On the completion path, `$task-author` records the exact candidate and
   evidence with `nk task submit` and `nk task record-validation`; independent
   review stays internal and returns verified findings to the same author.
5. Resume blocked work with `nk task unblock <slug> --to Backlog|Ready`; use
   `nk task follow-up`, `nk task dependency`, and `nk task reorder` for queue
   maintenance.

Use [deep blocker diagnosis](skills/deep-blocker-diagnosis/SKILL.md) for durable
blocker investigation and
[workstream decoupling](skills/workstream-decoupling/SKILL.md) when one fault or
dependency chain holds unrelated work.

## Usage

Install on POSIX or Windows:

```sh
./install.sh
./install.ps1
```

Explore the top-level commands:

```sh
nk --help
nk task --help
nk scheduler --help
nk cluster --help
nk node --help
nk workspace --help
```

## License

This project is dedicated to the public domain under [CC0 1.0](LICENSE). Use
any part of it for any purpose. Permission and attribution are not required.
