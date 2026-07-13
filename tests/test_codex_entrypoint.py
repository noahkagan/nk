from __future__ import annotations

import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_support():
    path = (
        Path(__file__).resolve().parents[1]
        / "entrypoints"
        / "codex"
        / "codex_support.py"
    )
    return runpy.run_path(str(path), run_name="codex_exec_entrypoint")


class CodexEntrypointTests(unittest.TestCase):
    def test_command_accepts_the_same_skill_invocation_as_interactive_codex(self) -> None:
        module = load_support()

        self.assertEqual(
            module["build_command"]("$task-author task"),
            [
                "codex", "exec", "--enable", "enable_fanout",
                "--sandbox", "workspace-write", "$task-author task",
            ],
        )

    def test_command_enables_fanout(self) -> None:
        module = load_support()

        self.assertEqual(
            module["build_command"]("prompt"),
            [
                "codex",
                "exec",
                "--enable",
                "enable_fanout",
                "--sandbox",
                "workspace-write",
                "prompt",
            ],
        )

    def test_command_resumes_exact_session(self) -> None:
        module = load_support()

        self.assertEqual(
            module["build_command"]("prompt", "session-id"),
            [
                "codex", "exec", "--enable", "enable_fanout",
                "--sandbox", "workspace-write", "resume", "session-id",
                "prompt",
            ],
        )

    def test_entrypoint_reuses_discovered_session_on_next_run(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="nk-codex-resume-"))
        workspace = root / "workspace"
        workspace.mkdir()
        binary = root / "bin/codex"
        binary.parent.mkdir()
        log = root / "commands.jsonl"
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "home = pathlib.Path(os.environ['HOME'])\n"
            "session = home / '.codex/sessions/session.jsonl'\n"
            "session.parent.mkdir(parents=True, exist_ok=True)\n"
            "session.write_text(json.dumps({'type': 'session_meta', 'payload': "
            "{'originator': 'codex_exec', 'cwd': os.getcwd(), "
            "'session_id': 'session-id'}}) + '\\n')\n"
            "with pathlib.Path(os.environ['COMMAND_LOG']).open('a') as handle:\n"
            "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        prompt = root / "prompt.md"
        prompt.write_text("$task-author task\n", encoding="utf-8")
        session_file = root / "runtime/session.txt"
        entrypoint = Path(__file__).parents[1] / "entrypoints/codex/codex"
        environment = {
            **os.environ,
            "HOME": str(root),
            "PATH": str(binary.parent) + os.pathsep + os.environ["PATH"],
            "COMMAND_LOG": str(log),
            "NK_RUN_PROMPT_FILE": str(prompt),
            "NK_RUN_SESSION_FILE": str(session_file),
        }

        subprocess.run(
            [sys.executable, str(entrypoint)], cwd=workspace,
            env=environment, check=True,
        )
        subprocess.run(
            [sys.executable, str(entrypoint)], cwd=workspace,
            env=environment, check=True,
        )

        commands = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertNotIn("resume", commands[0])
        self.assertEqual(commands[1][-3:-1], ["resume", "session-id"])
        self.assertEqual(session_file.read_text(), "session-id\n")

    def test_windows_uses_native_harness(self) -> None:
        module = load_support()
        app_data = Path(tempfile.mkdtemp(prefix="nk-codex-app-data-"))
        executable = (
            app_data / "npm/node_modules/@openai/codex/vendor/codex.exe"
        )
        executable.parent.mkdir(parents=True)
        executable.touch()

        with mock.patch.dict(os.environ, {"APPDATA": str(app_data)}):
            self.assertEqual(
                module["codex_executable"]("nt"), str(executable)
            )

    def test_pty_output_can_be_forwarded_live(self) -> None:
        module = load_support()
        output = io.BytesIO()

        result = module["run_with_pty"](
            [sys.executable, "-c", 'print("progress", flush=True)'], output
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn(b"progress", output.getvalue())

    def test_windows_detached_run_closes_stdin(self) -> None:
        module = load_support()
        completed = subprocess.CompletedProcess(["codex"], 0, "", "")

        with (
            mock.patch.object(module["os"], "name", "nt"),
            mock.patch.object(
                module["subprocess"], "run", return_value=completed
            ) as run,
        ):
            module["run_with_pty"](["codex"], io.BytesIO())

        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_pty_command_uses_requested_workspace(self) -> None:
        module = load_support()
        workspace = Path(tempfile.mkdtemp(prefix="nk-codex-test-"))

        result = module["run_with_pty"](
            [sys.executable, "-c", "import os; print(os.getcwd())"],
            cwd=workspace,
        )

        self.assertIn(str(workspace), result.stderr)

    def test_session_discovery_reads_utf8_jsonl(self) -> None:
        module = load_support()
        home = Path(tempfile.mkdtemp(prefix="nk-codex-home-"))
        workspace = home / "wörkspace"
        workspace.mkdir()
        session = home / ".codex/sessions/session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "originator": "codex_exec", "cwd": str(workspace),
                "session_id": "séssion",
            },
        }, ensure_ascii=False) + "\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(
                module["discover_session_id"](workspace, session.stat().st_mtime),
                "séssion",
            )

    def test_session_discovery_skips_undecodable_newer_candidate(self) -> None:
        module = load_support()
        home = Path(tempfile.mkdtemp(prefix="nk-codex-home-"))
        workspace = home / "workspace"
        workspace.mkdir()
        sessions = home / ".codex/sessions"
        sessions.mkdir(parents=True)
        valid = sessions / "valid.jsonl"
        valid.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "originator": "codex_exec", "cwd": str(workspace),
                "session_id": "session-id",
            },
        }) + "\n", encoding="utf-8")
        invalid = sessions / "newer.jsonl"
        invalid.write_bytes(b"\x9d\n")
        os.utime(invalid, (valid.stat().st_mtime + 1, valid.stat().st_mtime + 1))

        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(
                module["discover_session_id"](workspace, valid.stat().st_mtime),
                "session-id",
            )


if __name__ == "__main__":
    unittest.main()
