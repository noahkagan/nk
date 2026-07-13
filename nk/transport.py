from __future__ import annotations

import shlex


LOCAL_TARGETS = {"localhost", "127.0.0.1", "::1"}


def is_local_target(target: str) -> bool:
    return target in LOCAL_TARGETS


def remote_prefix(target: str) -> list[str]:
    try:
        parts = shlex.split(target)
    except ValueError as exc:
        raise ValueError(f"invalid node target: {exc}") from exc
    if not parts:
        raise ValueError("invalid node target")
    return ["ssh", parts[0]] if len(parts) == 1 else parts


def remote_command(target: str, command: str) -> list[str]:
    return [*remote_prefix(target), command]


def scp_command(target: str, source: str, destination: str) -> list[str] | None:
    prefix = remote_prefix(target)
    if prefix[0] != "ssh":
        return None
    return ["scp", *prefix[1:-1], source, f"{prefix[-1]}:{destination}"]
