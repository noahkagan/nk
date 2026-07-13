---
name: map-workstreams
description: Create or refresh a human-oriented WORKSTREAMS.md that groups current tasks by outcome and visualizes dependencies, parallel width, resource demand, and a simplified equal-size Gantt without becoming authoritative. Use when a flat TODO is hard to reason about, when someone asks what active efforts add up to, or when planning agent capacity.
---

# Map workstreams

Read the active tracker entries and their task documents. Group them into a
few outcome-oriented workstreams using the project's language.

Write `WORKSTREAMS.md` as a shallow navigation and capacity aid:

- State prominently that it is human-oriented, possibly incomplete, and not a
  source of truth.
- Link to the authoritative tracker and task documents.
- Give each workstream one outcome sentence, a Mermaid dependency graph, and
  links to its current tasks. Point arrows from prerequisite to dependent.
- Derive edges and resource requirements from authoritative task files; do not
  maintain competing copies of lifecycle state, acceptance criteria,
  ownership, or evidence.
- Compute the dependency graph's maximum antichain as structural width and
  show one maximum-width cut grouped by resource type.
- Add a simplified Mermaid Gantt that treats every current task as one equal
  work unit and schedules it in the earliest dependency-allowed relative slot.
  State assumptions about unresolved blockers. Summarize concurrent tasks and
  scarce resources per slot.
- Distinguish structural width from the equal-size schedule's peak staffing:
  delaying independent tasks can increase overlap without increasing required
  ASAP capacity.
- Do not invent metadata, hierarchy, tooling, or task boundaries.
- Prefer omitting an unclear grouping to asserting a false relationship; mark
  a load-bearing uncertainty with `???` when it must remain visible.
- Verify every relative link, graph edge, task count, and reported width after
  writing.

Keep the document small enough to scan. Delete stale groupings and completed
tasks when refreshing it; history belongs in the authoritative task system.
