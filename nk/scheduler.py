from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import output, task
from .config import (
    ConfigError,
    cluster_names,
    find_node,
    get_cluster,
    home,
    load_cordons,
    parse_workspace,
    set_cordon,
)
from .setup import application_revision
from .transport import is_local_target, remote_command as remote_transport_command


QUEUE_VALUES = {None, "empty", "Blocked", "Authoring", "Ready", "Done", "Cancelled"}
RETRY_DELAY_SECONDS = 30
REMOTE_OBSERVATION_TIMEOUT_S = 30
APP_ROOT = Path(__file__).resolve().parents[1]


class WorkerFailure(RuntimeError):
    pass


class RoutingFailure(WorkerFailure):
    pass


class TransportFailure(ConfigError):
    pass


class WorkspaceFault(ConfigError):
    pass


@dataclass(frozen=True)
class Workspace:
    reference: str
    path: str
    node: str
    target: str
    capabilities: dict[str, str]
    resources: dict[str, int]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def runtime_path(workspace: Path, name: str) -> Path:
    return workspace / ".workspace" / "nk" / name


def path_is_link(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & 0x400)


def claim_temp_path(workspace: Path, claim_id: str) -> Path:
    if len(claim_id) != 32 or any(
        character not in "0123456789abcdef" for character in claim_id
    ):
        raise WorkerFailure("claim ID cannot name a temporary directory")
    root = runtime_path(workspace, "tmp")
    parents = (workspace / ".workspace", root.parent, root)
    linked = next((path for path in parents if path_is_link(path)), None)
    if linked is not None:
        raise WorkspaceFault(f"claim temporary directory parent is a link: {linked}")
    path = root / claim_id
    if path_is_link(path):
        raise WorkspaceFault(f"claim temporary directory is a link: {path}")
    return path


def workspaces(cluster: dict[str, Any], *, include_absent: bool = False) -> list[Workspace]:
    result: list[Workspace] = []
    for node in cluster["nodes"]:
        if not include_absent and node["state"] != "present":
            continue
        for item in node["workspaces"]:
            if not include_absent and item["state"] != "present":
                continue
            result.append(Workspace(
                reference=f"{item['name']}@{node['name']}",
                path=item["path"],
                node=node["name"],
                target=node["target"],
                capabilities=node["capabilities"],
                resources=node["resources"],
            ))
    return result


def is_local(target: str) -> bool:
    return is_local_target(target)


def remote_command(workspace: Workspace, arguments: list[str]) -> list[str]:
    if is_local(workspace.target):
        return arguments
    encoded = subprocess.list2cmdline(arguments) if workspace.capabilities["os"] == "windows" else shlex.join(arguments)
    return remote_transport_command(workspace.target, encoded)


def command_output(workspace: Workspace, arguments: list[str]) -> str:
    completed = subprocess.run(remote_command(workspace, arguments), text=True, capture_output=True)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if completed.returncode in {75, 255}:
            raise TransportFailure(f"{workspace.reference}: {detail}")
        if completed.returncode == 76:
            raise WorkspaceFault(f"{workspace.reference}: {detail}")
        raise ConfigError(f"{workspace.reference}: {detail}")
    return completed.stdout


async def command_output_async(workspace: Workspace, arguments: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *remote_command(workspace, arguments),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=REMOTE_OBSERVATION_TIMEOUT_S
        )
    except (asyncio.CancelledError, asyncio.TimeoutError) as error:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if isinstance(error, asyncio.TimeoutError):
            raise TransportFailure(
                f"{workspace.reference}: remote observation timed out"
            ) from error
        raise
    output = stdout.decode(errors="replace")
    if process.returncode:
        detail = stderr.decode(errors="replace").strip() or output.strip()
        if process.returncode in {75, 255}:
            raise TransportFailure(f"{workspace.reference}: {detail}")
        if process.returncode == 76:
            raise WorkspaceFault(f"{workspace.reference}: {detail}")
        raise ConfigError(f"{workspace.reference}: {detail}")
    return output


def nk_command() -> str:
    command = shutil.which("nk")
    if command:
        return command
    launcher = "nk.cmd" if os.name == "nt" else "nk"
    for source in (APP_ROOT / "bin" / launcher, APP_ROOT.parent / "bin" / launcher):
        if source.is_file():
            return str(source)
    raise ConfigError("nk is not on PATH")


def node_nk(workspace: Workspace) -> str:
    if is_local(workspace.target):
        return nk_command()
    return r".nk\bin\nk.cmd" if workspace.capabilities["os"] == "windows" else ".nk/bin/nk"


def node_probe(node: dict[str, Any]) -> Workspace:
    return Workspace(
        reference=f"NODE@{node['name']}", path="", node=node["name"],
        target=node["target"], capabilities=node["capabilities"],
        resources=node["resources"],
    )


async def read_node_revision(node: dict[str, Any]) -> str:
    probe = node_probe(node)
    value = (
        await command_output_async(
            probe, [node_nk(probe), "scheduler", "_revision"]
        )
    ).strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ConfigError(f"{probe.reference}: invalid installed nk revision")
    return value


async def verify_cluster_revision_async(cluster: dict[str, Any]) -> None:
    expected = application_revision()
    nodes = [node for node in cluster["nodes"] if node["state"] == "present"]
    for node in nodes:
        output.event(
            "CHECKING", "installed nk revision", workspace=f"NODE@{node['name']}"
        )
    values = await asyncio.gather(
        *(read_node_revision(node) for node in nodes), return_exceptions=True
    )
    failures = []
    for node, value in zip(nodes, values):
        if isinstance(value, BaseException):
            failures.append(str(value))
        elif value != expected:
            failures.append(
                f"NODE@{node['name']}: installed nk revision mismatch: "
                f"expected {expected}, found {value}"
            )
    if failures:
        raise ConfigError("; ".join(failures))


def verify_cluster_revision(cluster: dict[str, Any]) -> None:
    asyncio.run(verify_cluster_revision_async(cluster))


async def observe_node_states(
    items: list[Workspace],
) -> list[dict[str, Any] | None | ConfigError]:
    probe = Workspace(
        reference=f"NODE@{items[0].node}", path="", node=items[0].node,
        target=items[0].target, capabilities=items[0].capabilities,
        resources=items[0].resources,
    )
    arguments = [node_nk(probe), "scheduler", "_states"]
    for item in items:
        arguments.extend(("--workspace", item.reference, item.path))
    value = await command_output_async(probe, arguments)
    try:
        entries = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid run states from {probe.reference}") from exc
    if not isinstance(entries, list) or len(entries) != len(items):
        raise ConfigError(f"invalid run states from {probe.reference}")
    results: list[dict[str, Any] | None | ConfigError] = []
    for item, entry in zip(items, entries):
        if not isinstance(entry, dict) or entry.get("reference") != item.reference:
            raise ConfigError(f"invalid run states from {probe.reference}")
        if set(entry) == {"reference", "error"} and isinstance(entry["error"], str):
            results.append(ConfigError(f"{item.reference}: {entry['error']}"))
        elif set(entry) == {"reference", "state"} and (
            entry["state"] is None or isinstance(entry["state"], dict)
        ):
            results.append(entry["state"])
        else:
            raise ConfigError(f"invalid run states from {probe.reference}")
    return results


async def observe_states(
    items: list[Workspace],
) -> list[dict[str, Any] | None | BaseException]:
    groups: dict[tuple[str, str], list[Workspace]] = {}
    for item in items:
        groups.setdefault((item.node, item.target), []).append(item)
    batches = list(groups.values())
    for batch in batches:
        output.event(
            "CHECKING", "run states", workspace=f"NODE@{batch[0].node}",
            verbose=True,
        )
    observed = await asyncio.gather(
        *(observe_node_states(batch) for batch in batches),
        return_exceptions=True,
    )
    by_reference: dict[str, dict[str, Any] | None | BaseException] = {}
    for batch, result in zip(batches, observed):
        if isinstance(result, BaseException):
            for item in batch:
                by_reference[item.reference] = result
        else:
            for item, state_result in zip(batch, result):
                by_reference[item.reference] = state_result
    return [by_reference[item.reference] for item in items]


async def read_claims(workspace: Workspace) -> list[dict[str, str]]:
    output = await command_output_async(
        workspace,
        [node_nk(workspace), "scheduler", "_claims", "--workspace", workspace.path],
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid claims from {workspace.reference}") from exc
    if not isinstance(value, list):
        raise ConfigError(f"invalid claims from {workspace.reference}")
    return value


async def read_queues(workspace: Workspace) -> dict[str, list[str]]:
    output = await command_output_async(
        workspace,
        [node_nk(workspace), "scheduler", "_queues", "--workspace", workspace.path],
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid queue overview from {workspace.reference}") from exc
    if (
        not isinstance(value, dict)
        or set(value) != set(task.QUEUE_ORDER)
        or any(
            not isinstance(items, list)
            or any(not isinstance(slug, str) for slug in items)
            for items in value.values()
        )
    ):
        raise ConfigError(f"invalid queue overview from {workspace.reference}")
    return {queue: value[queue] for queue in task.QUEUE_ORDER}


def windows_process_alive(pid: int, kernel32: Any | None = None) -> bool:
    if kernel32 is None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        import ctypes

        from ctypes import wintypes

        exit_code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        return windows_process_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def raw_state(workspace: Path) -> dict[str, Any] | None:
    path = runtime_path(workspace, "run.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def state(workspace: Path, *, persist: bool = True) -> dict[str, Any] | None:
    path = runtime_path(workspace, "run.json")
    value = raw_state(workspace)
    if value is None:
        return None
    stale_reservation = value.get("state") == "reserved" and "pid" not in value
    dead_process = (
        value.get("state") in {"reserved", "running"}
        and "pid" in value
        and not process_alive(value.get("pid", -1))
    )
    if stale_reservation or dead_process:
        value["state"] = "interrupted"
        value["ended_at"] = utc_now()
        if persist:
            atomic_json(path, value)
    stopped_terminal = (
        value.get("state") in {"finishing", "failed", "interrupted"}
        and value.get("claim_id")
        and "pid" in value
        and not process_alive(value.get("pid", -1))
    )
    if stopped_terminal and persist:
        temporary = claim_temp_path(workspace, str(value.get("claim_id", "")))
        if temporary.exists():
            shutil.rmtree(temporary)
        if value["state"] == "finishing":
            value["state"] = "completed"
            value["ended_at"] = utc_now()
            atomic_json(path, value)
    return value


def queue_snapshot(workspace: Path) -> dict[str, Any]:
    control = task.resolve_default_branch(workspace)
    tree = task.fetch_ref(workspace, control.ref)
    try:
        task.synchronize_checkout(workspace, control, tree)
    except task.CoordinationError as exc:
        raise WorkspaceFault(str(exc)) from exc
    buckets, _ = task.parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
    claims = task.local_state(workspace)[2]
    claimed_slugs = {item["slug"] for item in claims}
    dependencies = task.dependencies_from_checkout(workspace, buckets)
    candidates = []
    occupied = []
    for claim in claims:
        slug = claim["slug"]
        resume_after = claim.get("resume_after")
        if resume_after and task.parse_resume_after(resume_after) > datetime.now(timezone.utc):
            occupied.append(claim["owner"])
            continue
        if any(buckets.get(value) != "Done" for value in dependencies.get(slug, [])):
            occupied.append(claim["owner"])
            continue
        manifest = task.manifest_from_checkout(workspace, slug, require_ready=True)
        candidates.append({
            "slug": slug,
            "capabilities": manifest["capabilities"],
            "resources": manifest["resources"],
            "workspace": claim["owner"],
        })
    for slug, queue in buckets.items():
        if queue != "Ready" or slug in claimed_slugs:
            continue
        if any(buckets.get(value) != "Done" for value in dependencies.get(slug, [])):
            continue
        manifest = task.manifest_from_checkout(
            workspace, slug, require_ready=True
        )
        item = {
            "slug": slug,
            "capabilities": manifest["capabilities"],
            "resources": manifest["resources"],
        }
        candidates.append(item)
    return {"candidates": candidates, "occupied": occupied}


def fits(candidate: dict[str, Any], workspace: Workspace, available_gpu: int) -> bool:
    capabilities = candidate["capabilities"]
    resources = candidate["resources"]
    return all(workspace.capabilities.get(key) == value for key, value in capabilities.items()) and resources.get("gpu", 0) <= available_gpu


async def read_queue(workspace: Workspace) -> dict[str, Any]:
    output = await command_output_async(workspace, [
        node_nk(workspace), "scheduler", "_snapshot", "--workspace", workspace.path,
    ])
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid queue snapshot from {workspace.reference}") from exc
    if not isinstance(value, dict) or set(value) != {"candidates", "occupied"}:
        raise ConfigError(f"invalid queue snapshot from {workspace.reference}")
    return value


def reserve_state(workspace: Workspace, slug: str, gpu: int) -> None:
    command_output(workspace, [
        node_nk(workspace), "scheduler", "_reserve", "--workspace", workspace.path,
        "--reference", workspace.reference,
        "--slug", slug, "--gpu", str(gpu),
    ])


def launch(workspace: Workspace, slug: str) -> None:
    command_output(workspace, [
        node_nk(workspace), "scheduler", "_launch", "--workspace", workspace.path,
        "--reference", workspace.reference,
        "--slug", slug,
    ])


def block_unfit(
    workspace: Workspace, slug: str, cordoned: list[str] | None = None
) -> None:
    arguments = [
        node_nk(workspace), "scheduler", "_block", "--workspace", workspace.path,
        "--slug", slug,
    ]
    for reference in cordoned or []:
        arguments.extend(("--cordoned", reference))
    command_output(workspace, arguments)


@contextmanager
def controller_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ConfigError("another scheduler or cluster setup owns the controller lock") from exc
        yield


async def schedule_once_async(
    cluster: dict[str, Any], retry_after: dict[str, float] | None = None,
    terminal_after: dict[str, tuple[str, float]] | None = None,
) -> int:
    retries = retry_after if retry_after is not None else {}
    terminals = terminal_after if terminal_after is not None else {}
    now = time.monotonic()
    declared = workspaces(cluster)
    cordons = load_cordons(cluster["name"])
    states: dict[str, dict[str, Any] | None] = {}
    reachable = []
    incomplete_nodes = set()
    checks = await observe_states(declared)
    for item, result in zip(declared, checks):
        if isinstance(result, ConfigError):
            incomplete_nodes.add(item.node)
            output.event(
                "UNAVAILABLE", result, workspace=item.reference, error=True
            )
        elif isinstance(result, BaseException):
            raise result
        else:
            states[item.reference] = result
            reachable.append(item)
    gpu_used: dict[str, int] = {}
    active_slugs: set[str] = set()
    free = []
    for item in reachable:
        current = states[item.reference]
        if current and current.get("state") in {
            "reserved", "running", "finishing", "interrupted",
        }:
            gpu_used[item.node] = gpu_used.get(item.node, 0) + int(current.get("gpu", 0))
            if isinstance(current.get("slug"), str):
                active_slugs.add(current["slug"])
        else:
            free.append(item)
        if current and current.get("state") == "failed":
            fingerprint = json.dumps({
                key: current.get(key)
                for key in ("state", "slug", "claim_id", "ended_at")
            }, sort_keys=True)
            previous = terminals.get(item.reference)
            if previous is None or previous[0] != fingerprint:
                terminals[item.reference] = (
                    fingerprint, now + RETRY_DELAY_SECONDS,
                )
    resumptions: dict[str, dict[str, Any]] = {}
    waiting: dict[str, dict[str, Any]] = {}
    unavailable = set()
    snapshot = None
    readers = [
        item for item in free
        if item.reference not in cordons and is_local(item.target)
    ] + [
        item for item in free
        if item.reference not in cordons and not is_local(item.target)
    ]
    reader = None
    for source in readers:
        output.event(
            "ELECTED", "task queue reader", workspace=source.reference,
            verbose=True,
        )
        output.event(
            "READING", "task queue", workspace=source.reference, verbose=True
        )
        try:
            snapshot = await read_queue(source)
        except TransportFailure as exc:
            unavailable.add(source.reference)
            output.event(
                "UNAVAILABLE", exc, workspace=source.reference, error=True
            )
            continue
        except WorkspaceFault as exc:
            reason = " ".join(str(exc).split())
            set_cordon(cluster["name"], source.reference, reason)
            cordons[source.reference] = reason
            output.event(
                "CORDONED", exc, workspace=source.reference, error=True
            )
            continue
        except ConfigError as exc:
            unavailable.add(source.reference)
            output.event(
                "UNAVAILABLE", exc, workspace=source.reference, error=True
            )
            continue
        reader = source
        break
    if snapshot is None:
        output.event("IDLE", "no task launched", verbose=True)
        return 0
    assert reader is not None
    for candidate in snapshot["candidates"]:
        if candidate["slug"] in active_slugs:
            continue
        destination = resumptions if candidate.get("workspace") else waiting
        destination.setdefault(candidate["slug"], candidate)
    occupied = set(snapshot["occupied"])
    free = [
        item for item in free
        if item.reference not in cordons
        and item.reference not in unavailable
        and item.reference not in occupied
    ]
    new_work = list(waiting.values())
    candidates = [*resumptions.values(), *new_work]

    def usage_key(value: dict[str, int]) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(value.items()))

    def available_gpu(item: Workspace, usage: dict[str, int]) -> int:
        if item.node in incomplete_nodes:
            return 0
        return item.resources.get("gpu", 0) - usage.get(item.node, 0)

    def can_place(
        candidate: dict[str, Any], item: Workspace, usage: dict[str, int]
    ) -> bool:
        return (
            retries.get(item.reference, 0) <= now
            and terminals.get(item.reference, ("", 0))[1] <= now
            and candidate.get("workspace") in {None, item.reference}
            and candidate.get("author_owner") != item.reference
            and fits(candidate, item, available_gpu(item, usage))
        )

    by_reference = {item.reference: item for item in free}
    score_cache: dict[
        tuple[int, tuple[str, ...], tuple[tuple[str, int], ...]],
        tuple[int, ...],
    ] = {}

    def future_score(
        index: int, free_references: tuple[str, ...],
        usage: tuple[tuple[str, int], ...],
    ) -> tuple[int, ...]:
        key = (index, free_references, usage)
        cached = score_cache.get(key)
        if cached is not None:
            return cached
        if index >= len(candidates):
            return ()
        candidate = candidates[index]
        current_usage = dict(usage)
        best = (0, *future_score(index + 1, free_references, usage))
        for reference in free_references:
            item = by_reference[reference]
            if not can_place(candidate, item, current_usage):
                continue
            next_usage = dict(current_usage)
            next_usage[item.node] = (
                next_usage.get(item.node, 0)
                + candidate["resources"].get("gpu", 0)
            )
            score = (
                1,
                *future_score(
                    index + 1,
                    tuple(value for value in free_references if value != reference),
                    usage_key(next_usage),
                ),
            )
            if score > best:
                best = score
        score_cache[key] = best
        return best

    def ordered_eligible(
        candidate_index: int, candidate: dict[str, Any]
    ) -> list[Workspace]:
        options = []
        for item in free:
            if not can_place(candidate, item, gpu_used):
                continue
            next_usage = dict(gpu_used)
            next_usage[item.node] = (
                next_usage.get(item.node, 0)
                + candidate["resources"].get("gpu", 0)
            )
            remaining_gpu = available_gpu(item, gpu_used) - candidate["resources"].get("gpu", 0)
            options.append((
                future_score(
                    candidate_index + 1,
                    tuple(
                        value.reference for value in free
                        if value.reference != item.reference
                    ),
                    usage_key(next_usage),
                ),
                (remaining_gpu, item.reference),
                item,
            ))
        # ponytail: brute-force only over currently free workspaces; use matching
        # if a placement turn ever has enough free workspaces for this to matter.
        options.sort(key=lambda value: value[1])
        options.sort(key=lambda value: value[0], reverse=True)
        return [item for _, _, item in options]

    launched = 0
    for candidate_index, candidate in enumerate(candidates):
        attempted = False
        while True:
            eligible = ordered_eligible(candidate_index, candidate)
            if not eligible:
                if not attempted and not any(
                    item.reference not in cordons
                    and candidate.get("workspace") in {None, item.reference}
                    and fits(candidate, item, item.resources.get("gpu", 0))
                    for item in declared
                ):
                    claimed_owner = next(
                        (
                            item for item in reachable
                            if item.reference == candidate.get("workspace")
                            and (states[item.reference] or {}).get("state")
                            not in {"reserved", "running", "finishing", "interrupted"}
                        ),
                        None,
                    )
                    selected = claimed_owner if candidate.get("workspace") else (
                        free[0] if free else None
                    )
                    if selected is not None:
                        cordoned_fit = sorted(
                            item.reference for item in declared
                            if item.reference in cordons
                            and candidate.get("workspace") in {None, item.reference}
                            and fits(candidate, item, item.resources.get("gpu", 0))
                        )
                        try:
                            output.event(
                                "BLOCKING", candidate["slug"], "no structural fit",
                                workspace=selected.reference,
                            )
                            if cordoned_fit:
                                block_unfit(selected, candidate["slug"], cordoned_fit)
                            else:
                                block_unfit(selected, candidate["slug"])
                        except ConfigError as exc:
                            output.event(
                                "UNAVAILABLE", exc, workspace=selected.reference,
                                error=True,
                            )
                        else:
                            output.event(
                                "BLOCKED", candidate["slug"],
                                "no declared workspace can satisfy requirements",
                                workspace=selected.reference, error=True,
                            )
                break
            attempted = True
            selected = eligible[0]
            gpu = candidate["resources"].get("gpu", 0)
            try:
                output.event(
                    "RESERVING", candidate["slug"],
                    workspace=selected.reference,
                )
                reserve_state(selected, candidate["slug"], gpu)
            except TransportFailure as exc:
                retries[selected.reference] = (
                    time.monotonic() + RETRY_DELAY_SECONDS
                )
                free.remove(selected)
                score_cache.clear()
                output.event(
                    "UNAVAILABLE", exc, workspace=selected.reference,
                    error=True,
                )
                continue
            except ConfigError as exc:
                reason = " ".join(str(exc).split())
                set_cordon(cluster["name"], selected.reference, reason)
                cordons[selected.reference] = reason
                free.remove(selected)
                score_cache.clear()
                output.event(
                    "CORDONED", exc, workspace=selected.reference,
                    error=True,
                )
                continue
            try:
                retries.pop(selected.reference, None)
                output.event(
                    "LAUNCHING", candidate["slug"],
                    workspace=selected.reference,
                )
                launch(selected, candidate["slug"])
            except ConfigError as exc:
                retries[selected.reference] = (
                    time.monotonic() + RETRY_DELAY_SECONDS
                )
                free.remove(selected)
                score_cache.clear()
                output.event(
                    "UNAVAILABLE", exc, workspace=selected.reference,
                    error=True,
                )
                continue
            gpu_used[selected.node] = gpu_used.get(selected.node, 0) + gpu
            score_cache.clear()
            terminals.pop(selected.reference, None)
            free.remove(selected)
            launched += 1
            output.event(
                "LAUNCHED", candidate["slug"], workspace=selected.reference,
            )
            break
    if launched == 0:
        output.event(
            "IDLE", "no task launched", workspace=reader.reference,
            verbose=True,
        )
    return launched


def schedule_once(
    cluster: dict[str, Any], retry_after: dict[str, float] | None = None,
    terminal_after: dict[str, tuple[str, float]] | None = None,
) -> int:
    return asyncio.run(schedule_once_async(cluster, retry_after, terminal_after))


def run(cluster_name: str | None, once: bool, verbose: bool) -> int:
    cluster = get_cluster(cluster_name)
    output.configure(
        (item.reference for item in workspaces(cluster)), verbose=verbose
    )
    state = home() / "clusters" / cluster["name"] / "state"
    output.log_to(state / "events.jsonl")
    lock = state / "controller.lock"
    try:
        with controller_lock(lock):
            verify_cluster_revision(cluster)
            retry_after: dict[str, float] = {}
            terminal_after: dict[str, tuple[str, float]] = {}
            output.event(
                "STARTED", cluster["name"],
                f"{len(workspaces(cluster))} workspaces",
            )
            try:
                while True:
                    launched = schedule_once(cluster, retry_after, terminal_after)
                    if once:
                        return 0
                    time.sleep(5)
            except KeyboardInterrupt:
                return 130
    finally:
        output.log_to(None)


def event_log_path(cluster_name: str | None) -> Path:
    cluster = get_cluster(cluster_name)
    return home() / "clusters" / cluster["name"] / "state" / "events.jsonl"


def render_status(rows: list[dict[str, Any]]) -> None:
    headers = ("WORKSPACE", "CLAIM ID", "RUN TASK", "RUN STATE", "SCHEDULING")
    rendered = [
        (
            row["workspace"],
            row["claim_id"] or "—",
            row["run_task"] or "—",
            row["run_state"],
            row["scheduling"],
        )
        for row in rows
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in rendered)])
        for index in range(len(headers))
    ]
    for row in (headers, *rendered):
        print("  ".join(
            value.ljust(widths[index]) if index < len(row) - 1 else value
            for index, value in enumerate(row)
        ))


async def status_snapshot(cluster: dict[str, Any]) -> dict[str, Any]:
    declared = workspaces(cluster)
    cordons = load_cordons(cluster["name"])
    states: dict[str, dict[str, Any] | None] = {}
    unavailable: set[str] = set()
    errors: list[str] = []
    failed = False

    def record_error(detail: str) -> None:
        nonlocal failed
        errors.append(detail)
        failed = True

    def record_states(
        items: list[Workspace], results: list[dict[str, Any] | None | BaseException]
    ) -> None:
        for item, result in zip(items, results):
            if isinstance(result, ConfigError):
                detail = str(result)
                if item.reference not in detail:
                    detail = f"{item.reference}: {detail}"
                record_error(detail)
                unavailable.add(item.reference)
            elif isinstance(result, BaseException):
                raise result
            else:
                states[item.reference] = result

    def claim_readers(items: list[Workspace]) -> list[Workspace]:
        return [
            item for item in items
            if item.reference not in unavailable
            and item.reference not in cordons
            and (states[item.reference] or {}).get("state")
            not in {"reserved", "running", "finishing", "interrupted"}
        ]

    async def read_from(readers: list[Workspace]) -> list[dict[str, str]] | None:
        for reader in readers:
            try:
                return await read_claims(reader)
            except ConfigError as exc:
                detail = str(exc)
                if reader.reference not in detail:
                    detail = f"{reader.reference}: {detail}"
                record_error(detail)
        return None

    local = [item for item in declared if is_local(item.target)]
    remote = [item for item in declared if not is_local(item.target)]
    remote_checks = asyncio.create_task(observe_states(remote))
    local_checks = await observe_states(local)
    record_states(local, local_checks)
    local_readers = claim_readers(local)
    claims, remote_results = await asyncio.gather(
        read_from(local_readers), remote_checks
    )
    record_states(remote, remote_results)
    remote_readers = claim_readers(remote)
    readers = [*local_readers, *remote_readers]
    if claims is None:
        claims = await read_from(remote_readers)
    if claims is None:
        record_error(
            "no idle workspace is available to read claims"
            if not readers else "claims could not be read"
        )

    claims_by_owner: dict[str, dict[str, str]] = {}
    declared_by_reference = {item.reference: item for item in declared}
    if claims is not None:
        for claim in claims:
            owner = claim["owner"]
            if owner in claims_by_owner:
                record_error(
                    f"workspace owns multiple claims: {owner}"
                )
            claims_by_owner[owner] = claim
            selected = declared_by_reference.get(owner)
            if selected is None:
                record_error(
                    f"claim belongs to undeclared workspace: "
                    f"{owner}: {claim['slug']}"
                )

    rows = []
    for item in declared:
        current = states.get(item.reference)
        claim = claims_by_owner.get(item.reference)
        claim_matches_run = bool(current and claim) and (
            current.get("claim_id") == claim["claim_id"]
            or (
                not current.get("claim_id")
                and current.get("slug") == claim["slug"]
            )
        )
        run_state = (current or {}).get("state")
        run_gpu = (
            int(current.get("gpu", 0))
            if isinstance(current, dict)
            and current.get("state") in {"reserved", "running", "finishing", "interrupted"}
            and isinstance(current.get("gpu", 0), int)
            else 0
        )
        show_run = bool(current) and (
            claims is None
            or claim_matches_run
            or run_state == "interrupted"
            or (
                claim is None
                and run_state in {"reserved", "running", "finishing"}
            )
        )
        claim_id = (
            (claim or {}).get("claim_id")
            or ((current or {}).get("claim_id") if show_run else None)
        )
        if item.reference in unavailable:
            run_slug, run_state = None, "unavailable"
        elif not show_run:
            run_slug, run_state = None, "idle"
        else:
            run_slug = str(current.get("slug")) if current.get("slug") else None
            run_state = str(current.get("state") or "unknown")
        scheduling = (
            f"cordoned: {cordons[item.reference]}"
            if item.reference in cordons else
            "cleanup pending" if run_state == "finishing" else
            "recovery required" if run_state == "interrupted" else "eligible"
        )
        rows.append({
            "workspace": item.reference,
            "node": item.node,
            "claim_id": str(claim_id) if claim_id else None,
            "claim_task": (claim or {}).get("slug"),
            "run_task": run_slug,
            "run_state": run_state,
            "run_gpu": run_gpu,
            "scheduling": scheduling,
        })
    return {
        "cluster": cluster["name"],
        "failed": failed,
        "errors": errors,
        "claims": sorted(claims_by_owner.values(), key=lambda claim: claim["owner"]),
        "workspaces": rows,
    }

async def status_async(cluster: dict[str, Any], *, json_output: bool = False) -> int:
    snapshot = await status_snapshot(cluster)
    if json_output:
        print(json.dumps(snapshot, separators=(",", ":")))
    else:
        for detail in snapshot["errors"]:
            print(f"nk scheduler: {detail}", file=sys.stderr)
        render_status(snapshot["workspaces"])
    return 1 if snapshot["failed"] else 0


def status(cluster_name: str | None, *, json_output: bool = False) -> int:
    return asyncio.run(status_async(get_cluster(cluster_name), json_output=json_output))


async def wait_async(cluster: dict[str, Any], interval: float = 2.0) -> int:
    declared = workspaces(cluster)
    output.configure(item.reference for item in declared)
    previous: tuple[tuple[str, str, str], ...] | None = None
    while True:
        checks = await observe_states(declared)
        failures = [
            str(result) for result in checks if isinstance(result, ConfigError)
        ]
        if failures:
            raise ConfigError(
                "cannot observe cluster drain: " + "; ".join(failures)
            )
        for result in checks:
            if isinstance(result, BaseException):
                raise result
        interrupted = [
            item.reference
            for item, result in zip(declared, checks)
            if isinstance(result, dict) and result.get("state") == "interrupted"
        ]
        if interrupted:
            raise ConfigError(
                "cannot observe cluster drain: interrupted run requires recovery: "
                + ", ".join(interrupted)
            )
        active = tuple(sorted(
            (
                item.reference,
                str(result.get("state")),
                str(result.get("slug") or "—"),
            )
            for item, result in zip(declared, checks)
            if isinstance(result, dict)
            and result.get("state") in {"reserved", "running", "finishing"}
        ))
        if not active:
            output.event("DRAINED", cluster["name"])
            return 0
        if active != previous:
            for reference, state_name, slug in active:
                output.event(
                    "DRAINING", slug, state_name, workspace=reference
                )
            previous = active
        await asyncio.sleep(interval)


def wait(cluster_name: str | None) -> int:
    try:
        return asyncio.run(wait_async(get_cluster(cluster_name)))
    except KeyboardInterrupt:
        return 130


def render_prompt(slug: str) -> str:
    path = APP_ROOT / "prompts" / "codex" / "author-goal.md"
    text = path.read_text(encoding="utf-8")
    return text.replace("{{claim_slug}}", slug)


def harness_command() -> list[str]:
    entrypoint = APP_ROOT / "entrypoints" / "codex" / "codex"
    if not entrypoint.is_file():
        raise ConfigError(f"Codex entrypoint is missing: {entrypoint}")
    return [sys.executable, str(entrypoint)]


def detached_process_options(platform_name: str) -> dict[str, Any]:
    if platform_name == "nt":
        return {"creationflags": 0x00000008 | 0x00000200 | 0x01000000}
    return {"start_new_session": True, "close_fds": True}


def create_windows_job(kernel32: Any | None = None) -> tuple[Any, Any]:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError("could not create harness Job Object")
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel32.CloseHandle(job)
        raise OSError("could not configure harness Job Object")
    return kernel32, job


def attach_windows_supervisor(kernel32: Any | None = None) -> tuple[Any, Any]:
    active_kernel, job = create_windows_job(kernel32)
    if not active_kernel.AssignProcessToJobObject(
        job, active_kernel.GetCurrentProcess()
    ):
        active_kernel.CloseHandle(job)
        raise OSError("could not assign supervisor to Job Object")
    return active_kernel, job


def worker(workspace: Path, reference: str, slug: str) -> int:
    run_path = runtime_path(workspace, "run.json")
    current = raw_state(workspace) or {}
    record = {
        **current, "state": "running", "pid": os.getpid(), "workspace": reference,
        "slug": slug, "started_at": utc_now(), "ended_at": None,
    }
    atomic_json(run_path, record)
    log_path = runtime_path(workspace, "harness.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    previous_owner = os.environ.get("NK_WORKSPACE_OWNER")
    os.environ["NK_WORKSPACE_OWNER"] = reference
    try:
        supervision = attach_windows_supervisor() if os.name == "nt" else None
        del supervision  # The process keeps the native handle open until exit.
        outcome = task.claim(workspace, slug, emit=False)
        if outcome["status"] not in {"claimed", "resumed"}:
            raise WorkerFailure(f"task could not be claimed: {slug}")
        record["claim_id"] = outcome["claim_id"]
        claim_temp = claim_temp_path(workspace, outcome["claim_id"])
        session_file = runtime_path(workspace, "sessions") / f"{outcome['claim_id']}.txt"
        claim_temp.mkdir(parents=True, exist_ok=True)
        record["temp_path"] = str(claim_temp)
        atomic_json(run_path, record)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        with tempfile.TemporaryDirectory(
            prefix="nk-control-", dir=claim_temp,
        ) as directory:
            checkpoint_before = max(
                task.checkpoint_numbers(workspace, slug), default=0
            )
            temporary = Path(directory)
            prompt = temporary / "prompt.md"
            metadata = temporary / "metadata.json"
            prompt.write_text(render_prompt(slug), encoding="utf-8")
            environment = os.environ.copy()
            environment.update({
                "NK_RUN_PROMPT_FILE": str(prompt),
                "NK_RUN_METADATA_FILE": str(metadata),
                "NK_RUN_SESSION_FILE": str(session_file),
                "NK_RUN_TEMP": str(claim_temp),
                "TMPDIR": str(claim_temp),
                "TMP": str(claim_temp),
                "TEMP": str(claim_temp),
            })
            with log_path.open("w", encoding="utf-8") as output:
                completed = subprocess.run(
                    harness_command(), cwd=workspace, env=environment,
                    stdout=output, stderr=subprocess.STDOUT,
                )
            buckets, _ = task.parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"))
            queue = buckets.get(slug)
            if completed.returncode or queue not in QUEUE_VALUES - {None, "empty"}:
                raise WorkerFailure("harness turn did not publish valid task state")
            if queue == "Ready":
                raise RoutingFailure("author turn cannot route a task to Ready")
            if queue == "Authoring":
                _, _, current_claim = task.task_claim(workspace, slug)
                checkpoint_after = max(
                    task.checkpoint_numbers(workspace, slug), default=0
                )
                try:
                    task.ensure_published(workspace)
                except task.CoordinationError as exc:
                    raise RoutingFailure(str(exc)) from exc
                if current_claim["claim_id"] != outcome["claim_id"]:
                    raise RoutingFailure("author turn replaced its claim")
                if checkpoint_after <= checkpoint_before:
                    raise RoutingFailure(
                        "author turn remained Authoring without a Checkpoint"
                    )
            value = {"slug": slug, "queue": queue}
            if queue != "Authoring":
                session_file.unlink(missing_ok=True)
        record.update({"state": "finishing", "result": value, "ended_at": None})
        atomic_json(run_path, record)
        return 0
    except RoutingFailure as exc:
        record.update({"state": "interrupted", "error": str(exc), "ended_at": utc_now()})
        atomic_json(run_path, record)
        with log_path.open("a", encoding="utf-8") as output:
            output.write(f"\nERROR: {exc}\n")
        return 1
    except Exception as exc:
        record.update({"state": "failed", "error": str(exc), "ended_at": utc_now()})
        atomic_json(run_path, record)
        with log_path.open("a", encoding="utf-8") as output:
            output.write(f"\nERROR: {exc}\n")
        return 1
    finally:
        if previous_owner is None:
            os.environ.pop("NK_WORKSPACE_OWNER", None)
        else:
            os.environ["NK_WORKSPACE_OWNER"] = previous_owner


def launch_worker(args: argparse.Namespace) -> int:
    command = [
        sys.executable, "-m", "nk", "scheduler", "_worker", "--workspace", args.workspace,
        "--reference", args.reference, "--slug", args.slug,
    ]
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(APP_ROOT) if not existing else str(APP_ROOT) + os.pathsep + existing
    )
    process_options = detached_process_options(os.name)
    with open(os.devnull, "rb") as input_file, open(os.devnull, "ab") as output:
        process = subprocess.Popen(
            command, stdin=input_file, stdout=output, stderr=output,
            env=environment, **process_options,
        )
    workspace = Path(args.workspace).expanduser()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = raw_state(workspace) or {}
        if current.get("pid") == process.pid and current.get("state") in {
            "running", "finishing", "completed", "failed",
        }:
            return 0
        if process.poll() is not None:
            raise ConfigError("agent supervisor exited before recording its state")
        time.sleep(0.01)
    process.terminate()
    raise ConfigError("agent supervisor did not record its state")


def workspace_logs(selected: Workspace, follow: bool, tail: int) -> int:
    command = [
        node_nk(selected), "scheduler", "_logs", "--workspace", selected.path,
        "--tail", str(tail),
    ] + (["-f"] if follow else [])
    return subprocess.run(remote_command(selected, command)).returncode


def logs(cluster_name: str | None, reference: str, follow: bool, tail: int) -> int:
    cluster = get_cluster(cluster_name)
    workspace_name, node_name = parse_workspace(reference)
    node = find_node(cluster, node_name)
    item = next((value for value in node["workspaces"] if value["name"] == workspace_name and value["state"] == "present"), None)
    if item is None:
        raise ConfigError(f"workspace does not exist: {reference}")
    selected = next(value for value in workspaces(cluster) if value.reference == reference)
    return workspace_logs(selected, follow, tail)


def recover(cluster_name: str | None, reference: str) -> int:
    cluster = get_cluster(cluster_name)
    workspace_name, node_name = parse_workspace(reference)
    node = find_node(cluster, node_name)
    item = next((value for value in node["workspaces"] if value["name"] == workspace_name and value["state"] == "present"), None)
    if item is None:
        raise ConfigError(f"workspace does not exist: {reference}")
    selected = next(value for value in workspaces(cluster) if value.reference == reference)
    command_output(selected, [
        node_nk(selected), "scheduler", "_recover",
        "--workspace", selected.path,
    ])
    return 0


async def resolve_claim_workspace(
    claim_id: str, clusters: list[dict[str, Any]]
) -> Workspace:
    clustered = [(cluster, workspaces(cluster)) for cluster in clusters]
    declared = [item for _, items in clustered for item in items]
    checks = await observe_states(declared)
    for result in checks:
        if isinstance(result, BaseException) and not isinstance(result, ConfigError):
            raise result
    matches = [
        item for item, result in zip(declared, checks)
        if isinstance(result, dict) and result.get("claim_id") == claim_id
    ]
    if len(matches) > 1:
        raise ConfigError(f"claim ID appears in multiple workspaces: {claim_id}")
    if matches:
        return matches[0]

    unavailable = [
        item.reference for item, result in zip(declared, checks)
        if isinstance(result, ConfigError)
    ]
    offset = 0
    for cluster, items in clustered:
        results = checks[offset:offset + len(items)]
        offset += len(items)
        readers = [
            item for item, result in zip(items, results)
            if not isinstance(result, BaseException)
            and (result or {}).get("state") not in {"reserved", "running", "finishing"}
        ]
        readers.sort(key=lambda item: (not is_local(item.target), items.index(item)))
        claims = None
        for reader in readers:
            try:
                claims = await read_claims(reader)
                break
            except ConfigError:
                unavailable.append(reader.reference)
        if claims is None:
            unavailable.append(f"claim snapshot for {cluster['name']}")
            continue
        owners = [claim["owner"] for claim in claims if claim["claim_id"] == claim_id]
        matches.extend(item for item in items if item.reference in owners)

    if len(matches) > 1:
        raise ConfigError(f"claim ID appears in multiple workspaces: {claim_id}")
    if matches:
        return matches[0]
    detail = f"; unavailable: {', '.join(unavailable)}" if unavailable else ""
    raise ConfigError(f"claim ID not found: {claim_id}{detail}")


def claim_logs(
    cluster_name: str | None, claim_id: str, follow: bool, tail: int
) -> int:
    names = [cluster_name] if cluster_name is not None else cluster_names()
    if not names:
        raise ConfigError("claim ID not found: no present clusters")
    selected = asyncio.run(resolve_claim_workspace(
        claim_id, [get_cluster(name) for name in names]
    ))
    return workspace_logs(selected, follow, tail)


def show_file(path: Path, follow: bool, tail: int, missing: str) -> int:
    if not path.is_file():
        raise ConfigError(missing)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    with path.open(encoding="utf-8", errors="replace") as handle:
        lines: deque[str] = deque(handle, maxlen=tail)
        sys.stdout.writelines(lines)
        sys.stdout.flush()
        while follow:
            chunk = handle.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            else:
                time.sleep(0.2)
    return 0


def show_log(workspace: Path, follow: bool, tail: int) -> int:
    return show_file(
        runtime_path(workspace, "harness.log"), follow, tail,
        f"workspace has no harness log: {workspace}",
    )


def events(cluster_name: str | None, follow: bool, tail: int) -> int:
    return show_file(
        event_log_path(cluster_name), follow, tail,
        "cluster has no scheduler event log",
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="nk scheduler")
    commands = root.add_subparsers(dest="command", required=True)
    run_command = commands.add_parser("run")
    run_command.add_argument("--cluster")
    run_command.add_argument("-v", "--verbose", action="store_true")
    run_command.add_argument("--once-through", action="store_true", help=argparse.SUPPRESS)
    status_command = commands.add_parser("status")
    status_command.add_argument("--cluster")
    status_command.add_argument("--json", action="store_true")
    wait_command = commands.add_parser("wait")
    wait_command.add_argument("--cluster")
    events_command = commands.add_parser("events")
    events_command.add_argument("--cluster")
    events_command.add_argument("-f", "--follow", action="store_true")
    events_command.add_argument("--tail", type=int, default=50)
    recover_command = commands.add_parser("recover")
    recover_command.add_argument("workspace")
    recover_command.add_argument("--cluster")
    logs_command = commands.add_parser("logs")
    logs_command.add_argument("claim_id", nargs="?")
    logs_command.add_argument("--cluster")
    logs_command.add_argument("--workspace")
    logs_command.add_argument("-f", "--follow", action="store_true")
    logs_command.add_argument("--tail", type=int, default=50)
    return root


def parse_internal(arguments: list[str]) -> argparse.Namespace:
    name, rest = arguments[0], arguments[1:]
    if name not in {"_states", "_claims", "_queues", "_snapshot", "_reserve", "_recover", "_launch", "_worker", "_logs", "_block", "_revision"}:
        raise ConfigError(f"unknown internal scheduler command: {name}")
    command = argparse.ArgumentParser(prog=f"nk scheduler {name}")
    if name == "_states":
        command.add_argument("--workspace", action="append", nargs=2, required=True)
    elif name != "_revision":
        command.add_argument("--workspace", required=True)
    if name == "_block":
        command.add_argument("--slug", required=True)
        command.add_argument("--cordoned", action="append", default=[])
    if name == "_logs":
        command.add_argument("--tail", type=int, default=50)
        command.add_argument("-f", "--follow", action="store_true")
    if name in {"_reserve", "_launch", "_worker"}:
        command.add_argument("--reference", required=True)
        command.add_argument("--slug", required=True)
    if name == "_reserve":
        command.add_argument("--gpu", type=int, required=True)
    result = command.parse_args(rest)
    result.command = name
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parse_internal(arguments) if arguments and arguments[0].startswith("_") else parser().parse_args(arguments)
    try:
        if args.command == "run":
            return run(args.cluster, args.once_through, args.verbose)
        if args.command == "status":
            return status(args.cluster, json_output=args.json)
        if args.command == "wait":
            return wait(args.cluster)
        if args.command == "events":
            if args.tail < 0:
                raise ConfigError("--tail must be nonnegative")
            return events(args.cluster, args.follow, args.tail)
        if args.command == "recover":
            return recover(args.cluster, args.workspace)
        if args.command == "logs":
            if args.tail < 0:
                raise ConfigError("--tail must be nonnegative")
            if bool(args.claim_id) == bool(args.workspace):
                raise ConfigError("provide a claim ID or --workspace, not both")
            if args.claim_id:
                return claim_logs(
                    args.cluster, args.claim_id, args.follow, args.tail
                )
            return logs(args.cluster, args.workspace, args.follow, args.tail)
        if args.command == "_revision":
            print(application_revision())
            return 0
        if args.command == "_states":
            values = []
            for reference, path in args.workspace:
                try:
                    values.append({"reference": reference, "state": state(Path(path).expanduser())})
                except Exception as exc:
                    values.append({"reference": reference, "error": str(exc)})
            print(json.dumps(values, separators=(",", ":")))
            return 0
        workspace = Path(args.workspace).expanduser()
        if args.command == "_claims":
            value = task.claim_snapshot(workspace)
            print(json.dumps(value, separators=(",", ":")))
            return 0
        if args.command == "_queues":
            value = task.queue_overview_from_tree(workspace)
            print(json.dumps(value, separators=(",", ":")))
            return 0
        if args.command == "_snapshot":
            with task.coordination_lock(workspace):
                value = queue_snapshot(workspace)
            print(json.dumps(value, separators=(",", ":")))
            return 0
        if args.command == "_reserve":
            previous_owner = os.environ.get("NK_WORKSPACE_OWNER")
            try:
                os.environ["NK_WORKSPACE_OWNER"] = args.reference
                with task.coordination_lock(workspace):
                    control = task.resolve_default_branch(workspace)
                    remote = task.fetch_ref(workspace, control.ref)
                    task.synchronize_checkout(workspace, control, remote)
                    buckets, _, claims = task.local_state(workspace)
                    claim = next(
                        (
                            value for value in claims
                            if value["slug"] == args.slug
                            and value["owner"] == args.reference
                            and buckets.get(args.slug) == "Authoring"
                        ),
                        None,
                    )
                    try:
                        if claim is None:
                            task.prepare_children(workspace)
                        else:
                            task.prepare_children(workspace, claim=claim)
                    except task.CandidatePreparationError:
                        pass
            finally:
                if previous_owner is None:
                    os.environ.pop("NK_WORKSPACE_OWNER", None)
                else:
                    os.environ["NK_WORKSPACE_OWNER"] = previous_owner
            atomic_json(runtime_path(workspace, "run.json"), {
                "state": "reserved", "workspace": args.reference, "slug": args.slug,
                "gpu": args.gpu, "reserved_at": utc_now(),
            })
            return 0
        if args.command == "_recover":
            current = state(workspace)
            if current is None:
                raise ConfigError(f"workspace has no run to recover: {workspace}")
            if current.get("state") != "interrupted":
                raise ConfigError(
                    f"workspace run is not interrupted: {workspace}: "
                    f"{current.get('state') or 'unknown'}"
                )
            runtime_path(workspace, "run.json").unlink(missing_ok=True)
            return 0
        if args.command == "_block":
            blocker = workspace / "scratch" / args.slug / "blocker.md"
            message = "No uncordoned workspace can satisfy this task's requirements."
            if args.cordoned:
                message += "\n\nCordoned workspaces that otherwise fit: " + ", ".join(args.cordoned) + "."
            previous = blocker.read_bytes() if blocker.exists() else None
            blocker.write_text(message + "\n", encoding="utf-8")
            try:
                task.block(workspace, args.slug)
            except Exception:
                if previous is None:
                    blocker.unlink(missing_ok=True)
                else:
                    blocker.write_bytes(previous)
                raise
            return 0
        if args.command == "_logs":
            return show_log(workspace, args.follow, args.tail)
        if args.command == "_launch":
            return launch_worker(args)
        if args.command == "_worker":
            return worker(workspace, args.reference, args.slug)
    except task.RemoteAccessError as exc:
        print(f"nk scheduler: {exc}", file=sys.stderr)
        return 75
    except WorkspaceFault as exc:
        print(f"nk scheduler: {exc}", file=sys.stderr)
        return 76
    except (ConfigError, task.CoordinationError, WorkerFailure, OSError, json.JSONDecodeError) as exc:
        print(f"nk scheduler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
