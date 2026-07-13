from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import BinaryIO

if os.name == "posix":
    import pty
    import select


def codex_executable(platform_name: str = os.name) -> str:
    if platform_name != "nt":
        return "codex"
    app_data = os.environ.get("APPDATA")
    if app_data:
        package = Path(app_data) / "npm/node_modules/@openai/codex"
        candidates = list(package.rglob("codex.exe")) if package.is_dir() else []
        if len(candidates) == 1:
            return str(candidates[0])
    return "codex.exe"


def build_command(prompt: str, session_id: str | None = None) -> list[str]:
    command = [
        codex_executable(),
        "exec",
        "--enable",
        "enable_fanout",
        "--sandbox",
        "workspace-write",
    ]
    if session_id:
        command.extend(("resume", session_id))
    command.append(prompt)
    return command


def output_tail(text: str, limit: int = 800) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return "..." + cleaned[-limit:]


def run_with_pty(
    command: list[str], stream: BinaryIO | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    if os.name != "posix":
        if stream is None:
            return subprocess.run(
                command, check=False, text=True, capture_output=True,
                stdin=subprocess.DEVNULL, env=env, cwd=cwd,
            )
        return subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=cwd,
        )

    master, slave = pty.openpty()
    output: list[str] = []

    def emit(chunk: bytes) -> None:
        if stream is None:
            output.append(chunk.decode(errors="replace"))
        else:
            stream.write(chunk)
            stream.flush()

    process = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        cwd=cwd,
        close_fds=True,
    )
    os.close(slave)
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    emit(chunk)
            if process.poll() is not None:
                while True:
                    ready, _, _ = select.select([master], [], [], 0)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    emit(chunk)
                return subprocess.CompletedProcess(
                    command, process.returncode, "", "".join(output)
                )
    finally:
        os.close(master)


def discover_session_id(workspace: Path, started_epoch: float) -> str | None:
    sessions = Path.home() / ".codex" / "sessions"
    if not sessions.exists():
        return None
    candidates: list[tuple[float, Path]] = []
    for path in sessions.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= started_epoch - 1:
            candidates.append((mtime, path))
    for _, path in sorted(candidates, reverse=True):
        try:
            with path.open(encoding="utf-8") as handle:
                event = json.loads(handle.readline())
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if event.get("type") != "session_meta":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("originator") != "codex_exec":
            continue
        if Path(str(payload.get("cwd", ""))) != workspace:
            continue
        session_id = payload.get("session_id") or payload.get("id")
        return str(session_id) if session_id else None
    return None
