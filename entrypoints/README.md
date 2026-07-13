# Entrypoints

Entrypoint scripts adapt agent CLI harnesses to the scheduler contract. A harness named `codex` maps to `entrypoints/codex/codex` when that script exists; otherwise the scheduler runs `entrypoints/codex` or `codex` from `PATH`.

Codex support files:

- `codex/codex`: runtime adapter used by the scheduler.
- `codex/codex_bootstrap`: manual workspace check using the same launch policy.
- `codex/codex_support.py`: shared Codex command, PTY, session, and Git metadata helpers.

The scheduler provides these environment variables:

- `NK_RUN_PROMPT_FILE`
- `NK_RUN_METADATA_FILE`
- `NK_RUN_TEMP`

Entrypoints launch the harness with the supplied invocation and emit
human-readable progress on standard output or standard error. The scheduler
captures that output in the workspace's latest harness log and reads durable
task state after each turn.
