from __future__ import annotations

import json
import base64
import io
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from . import output, task
from .config import ConfigError, find_node, home
from .transport import (
    is_local_target,
    remote_command as remote_transport_command,
    scp_command,
)


MARKER = Path(".workspace/nk/ownership.json")
REMOTE_STATE_PROBE = r'''
import datetime, json, os, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
except FileNotFoundError:
    raise SystemExit(0)
if isinstance(value, dict) and value.get("state") in {"reserved", "running"}:
    pid = value.get("pid")
    alive = False
    if type(pid) is int and os.name == "nt":
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
        if handle:
            try:
                exit_code = wintypes.DWORD()
                alive = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
    elif type(pid) is int:
        try:
            os.kill(pid, 0)
            alive = True
        except PermissionError:
            alive = True
        except OSError:
            pass
    if not alive:
        value["state"] = "interrupted"
        value["ended_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        temporary = path + ".tmp-" + str(os.getpid())
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
print(json.dumps(value, separators=(",", ":")))
'''.strip()
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def source_revision(root: Path) -> str:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root, capture_output=True, text=True,
    )
    value = revision.stdout.strip()
    if (
        revision.returncode or status.returncode or status.stdout
        or SHA_RE.fullmatch(value) is None
    ):
        raise ConfigError("nk cluster setup requires a clean, committed nk checkout")
    return value


def application_revision() -> str:
    root = Path(__file__).resolve().parents[1]
    marker = root / "REVISION"
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        if SHA_RE.fullmatch(value) is None:
            raise ConfigError("nk cluster setup requires a clean, committed nk checkout")
        return value
    return source_revision(root)


class NodeUnreachable(ConfigError):
    pass


def git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", *arguments], cwd=path, text=True, capture_output=True)
    if completed.returncode:
        raise ConfigError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def marker(cluster: str, node: str, workspace: str, path: str) -> dict[str, str]:
    return {"cluster": cluster, "node": node, "workspace": workspace, "path": path}


def write_marker(root: Path, expected: dict[str, str]) -> None:
    path = root / MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")


def ignore_runtime_state(root: Path) -> None:
    exclude = Path(git(root, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = root / exclude
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".workspace/" in text.splitlines():
        return
    separator = "" if not text or text.endswith("\n") else "\n"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(f"{text}{separator}.workspace/\n", encoding="utf-8")


def synchronize_workspace(root: Path, workspace: str | None = None) -> None:
    if git(root, "status", "--porcelain=v1", "--untracked-files=no"):
        raise ConfigError(f"workspace has tracked changes: {root}")
    try:
        default = task.resolve_default_branch(root)
    except task.CoordinationError as exc:
        raise ConfigError(str(exc)) from exc
    if git(root, "branch", "--show-current") != default.name:
        raise ConfigError(f"workspace is not on its default branch: {root}")
    completed = stream_command(
        ["git", "pull", "--ff-only", "origin", default.name],
        cwd=root, workspace=workspace,
    )
    if completed.returncode:
        raise ConfigError(f"workspace synchronization failed: {root}")


def stream_command(
    arguments: list[str], *, cwd: Path | None = None, workspace: str | None = None
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        arguments, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    captured = []
    for line in process.stdout:
        captured.append(line)
        text = line.rstrip("\r\n")
        output.line(text, workspace=workspace)
    return subprocess.CompletedProcess(
        arguments, process.wait(), "".join(captured), ""
    )


def bootstrap(
    root: Path, operating_system: str, workspace: str | None = None
) -> None:
    if operating_system == "windows":
        script = root / "bootstrap.ps1"
        if script.is_file():
            completed = stream_command(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                cwd=root, workspace=workspace,
            )
            if completed.returncode:
                raise ConfigError(f"workspace bootstrap failed: {root}")
        return
    script = root / "bootstrap.sh"
    if script.is_file() and stream_command(
        [str(script)], cwd=root, workspace=workspace
    ).returncode:
        raise ConfigError(f"workspace bootstrap failed: {root}")


def node_bootstrap(
    root: Path, operating_system: str, workspace: str | None = None
) -> None:
    if operating_system == "windows":
        script = root / "node-bootstrap.ps1"
        if script.is_file():
            completed = stream_command(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                cwd=root, workspace=workspace,
            )
            if completed.returncode:
                raise ConfigError(f"node bootstrap failed: {root}")
        return
    script = root / "node-bootstrap.sh"
    if script.is_file() and stream_command(
        [str(script)], cwd=root, workspace=workspace
    ).returncode:
        raise ConfigError(f"node bootstrap failed: {root}")


def materialize(
    cluster: str,
    repository: str,
    node: str,
    workspace: str,
    path: str,
    operating_system: str,
    previous_owners: tuple[str, ...] = (),
    log_workspace: str | None = None,
    bootstrap_node: bool = False,
) -> None:
    root = Path(path).expanduser()
    identity = f"{workspace}@{node}"
    expected = marker(cluster, node, workspace, path)
    if root.exists():
        if not (root / ".git").exists():
            raise ConfigError(f"existing workspace is not a Git checkout: {path}")
        origin = git(root, "remote", "get-url", "origin")
        if origin != repository:
            raise ConfigError(f"workspace origin differs from cluster repository: {path}")
    else:
        root.parent.mkdir(parents=True, exist_ok=True)
        completed = stream_command(
            ["git", "clone", repository, str(root)], workspace=log_workspace
        )
        if completed.returncode:
            raise ConfigError(f"could not clone workspace: {path}")
    synchronize_workspace(root, log_workspace)
    ignore_runtime_state(root)
    try:
        _, _, claims = task.local_state(root)
    except task.CoordinationError as exc:
        raise ConfigError(str(exc)) from exc
    marker_path = root / MARKER
    historical_owners = set(previous_owners)
    if marker_path.exists():
        try:
            previous = json.loads(marker_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"workspace ownership marker is invalid: {path}") from exc
        if isinstance(previous, dict):
            previous_identity = f"{previous.get('workspace')}@{previous.get('node')}"
            if previous_identity != identity:
                historical_owners.add(previous_identity)
    if any(claim["owner"] in historical_owners for claim in claims):
        raise ConfigError(f"previous workspace identity owns an active task claim: {path}")
    if bootstrap_node:
        node_bootstrap(root, operating_system, log_workspace)
    bootstrap(root, operating_system, log_workspace)
    write_marker(root, expected)


def repository_safe_for_removal(repository: Path) -> None:
    if git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ConfigError(f"workspace repository is dirty: {repository}")
    if git(repository, "stash", "list"):
        raise ConfigError(f"workspace repository has stashed work: {repository}")
    git(repository, "fetch", "--all", "--prune")
    try:
        default = task.resolve_default_branch(repository)
    except task.CoordinationError as exc:
        raise ConfigError(str(exc)) from exc
    branch = git(repository, "branch", "--show-current")
    if branch != default.name:
        raise ConfigError(
            f"workspace repository is not on remote default branch {default.name}: {repository}"
        )
    try:
        remote = task.fetch_ref(repository, default.ref)
    except task.CoordinationError as exc:
        raise ConfigError(str(exc)) from exc
    if git(repository, "rev-parse", "HEAD") != remote:
        raise ConfigError(f"workspace repository has unpublished commits: {repository}")
    local_refs = {
        ref: sha
        for ref, sha in (
            line.split("\t", 1)
            for line in git(
                repository, "for-each-ref", "--format=%(refname)\t%(objectname)",
                "refs/heads", "refs/tags",
            ).splitlines()
        )
    }
    published_refs: dict[str, set[str]] = {}
    for remote_name in git(repository, "remote").splitlines():
        for line in git(
            repository, "ls-remote", "--heads", "--tags", "--refs", remote_name
        ).splitlines():
            sha, ref = line.split("\t", 1)
            published_refs.setdefault(ref, set()).add(sha)
    for ref, sha in local_refs.items():
        if sha not in published_refs.get(ref, set()):
            raise ConfigError(f"workspace repository has unpublished ref {ref}: {repository}")
    published_shas = sorted(
        sha
        for values in published_refs.values()
        for sha in values
        if subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repository,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    )
    unpublished = git(repository, "rev-list", "--all", "--not", *published_shas)
    if unpublished:
        raise ConfigError(f"workspace repository has unpublished commits: {repository}")


def repositories_for_removal(root: Path) -> list[Path]:
    repositories = [root]
    meta_path = root / ".meta"
    if not meta_path.exists():
        return repositories
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError("workspace .meta is invalid") from exc
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        raise ConfigError("workspace .meta must contain a projects object")
    for relative in projects:
        child = root / relative
        if child.exists():
            if not (child / ".git").exists():
                raise ConfigError(f"registered child is not a Git checkout: {relative}")
            repositories.append(child)
    return repositories


def validate_removal(cluster: str, node: str, workspace: str, path: str) -> Path | None:
    root = Path(path).expanduser()
    if not root.exists():
        return None
    expected = marker(cluster, node, workspace, path)
    try:
        actual = json.loads((root / MARKER).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ConfigError(f"workspace ownership marker is missing or invalid: {path}") from exc
    if actual != expected:
        raise ConfigError(f"workspace ownership marker does not match: {path}")
    run_path = root / ".workspace" / "nk" / "run.json"
    if run_path.exists():
        try:
            state = json.loads(run_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"workspace run state is invalid: {path}") from exc
        if state.get("state") in {"reserved", "running"}:
            raise ConfigError(f"workspace has an active agent run: {path}")
    try:
        _, _, claims = task.local_state(root)
    except task.CoordinationError as exc:
        raise ConfigError(str(exc)) from exc
    identity = f"{workspace}@{node}"
    if any(claim["owner"] == identity for claim in claims):
        raise ConfigError(f"workspace owns an active task claim: {path}")
    for repository in repositories_for_removal(root):
        repository_safe_for_removal(repository)
    return root


def remove_materialized(cluster: str, node: str, workspace: str, path: str) -> None:
    root = validate_removal(cluster, node, workspace, path)
    if root is None:
        return
    shutil.rmtree(root)


def remote_executable(operating_system: str) -> str:
    return r".nk\bin\nk.cmd" if operating_system == "windows" else ".nk/bin/nk"


def remote_command(arguments: list[str], operating_system: str) -> str:
    if operating_system == "windows":
        script = "& " + " ".join(
            "'" + argument.replace('"', r'\"').replace("'", "''") + "'"
            for argument in arguments
        )
        return powershell(script)
    return shlex.join(arguments)


def powershell(command: str) -> str:
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"


def deployment_archive(revision: str | None = None) -> Path:
    application = Path(__file__).resolve().parents[1]
    distribution = application if (application / "bin").is_dir() else application.parent
    handle, name = tempfile.mkstemp(prefix="nk-node-", suffix=".tar")
    os.close(handle)
    archive = Path(name)

    def clean(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if "__pycache__" in info.name or info.name.endswith((".pyc", ".pyo")) else info

    with tarfile.open(archive, "w") as bundle:
        bundle.add(application / "nk", "app/nk", filter=clean)
        for name in ("entrypoints", "prompts"):
            bundle.add(application / name, f"app/{name}", filter=clean)
        bundle.add(distribution / "bin", "bin", filter=clean)
        bundle.add(distribution / "skills", "skills", filter=clean)
        encoded = ((revision or application_revision()) + "\n").encode("ascii")
        info = tarfile.TarInfo("app/REVISION")
        info.size = len(encoded)
        bundle.addfile(info, io.BytesIO(encoded))
    return archive


def deploy_remote(target: str, operating_system: str, revision: str) -> None:
    archive = deployment_archive(revision)
    try:
        copy_command = scp_command(target, str(archive), ".nk-install.tar")
        if copy_command is None:
            if operating_system == "windows":
                raise ConfigError("custom SSH wrappers cannot deploy Windows nodes")
            copied = subprocess.run(
                remote_transport_command(target, "cat > ~/.nk-install.tar"),
                input=archive.read_bytes(),
                capture_output=True,
            )
        else:
            copied = subprocess.run(copy_command, capture_output=True, text=True)
        if copied.returncode:
            stderr = copied.stderr.decode(errors="replace") if isinstance(copied.stderr, bytes) else copied.stderr
            stdout = copied.stdout.decode(errors="replace") if isinstance(copied.stdout, bytes) else copied.stdout
            raise ConfigError(stderr.strip() or stdout.strip())
        if operating_system == "windows":
            install = (
                "$root=Join-Path $HOME '.nk'; "
                "$agents=Join-Path $HOME '.agents\\skills'; $claude=Join-Path $HOME '.claude\\skills'; "
                "New-Item -ItemType Directory -Force -Path $root,(Join-Path $root 'clusters'),$agents,$claude | Out-Null; "
                "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root 'app'),(Join-Path $root 'bin'),(Join-Path $root 'skills'); "
                "tar -xf (Join-Path $HOME '.nk-install.tar') -C $root; "
                "Get-ChildItem (Join-Path $root 'skills') -Directory | ForEach-Object { "
                "$name=$_.Name; foreach ($discovery in @($agents,$claude)) { "
                "$destination=Join-Path $discovery $name; Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $destination; "
                "Copy-Item -Recurse $_.FullName $destination } }; "
                "Remove-Item (Join-Path $HOME '.nk-install.tar')"
            )
            install = powershell(install)
        else:
            install = (
                "mkdir -p ~/.nk/clusters && rm -rf ~/.nk/app ~/.nk/bin ~/.nk/skills "
                "&& tar -xf ~/.nk-install.tar -C ~/.nk && rm ~/.nk-install.tar "
                "&& chmod +x ~/.nk/bin/nk "
                "&& mkdir -p ~/.agents/skills ~/.claude/skills "
                "&& for skill in ~/.nk/skills/*; do name=$(basename \"$skill\"); "
                "for discovery in ~/.agents/skills ~/.claude/skills; do "
                "destination=\"$discovery/$name\"; rm -rf \"$destination\"; "
                "ln -sfn \"$skill\" \"$destination\"; done; done"
            )
        run_remote(target, install)
    finally:
        archive.unlink(missing_ok=True)


def run_remote(
    target: str, command: str, *, workspace: str | None = None
) -> subprocess.CompletedProcess[str]:
    arguments = remote_transport_command(target, command)
    completed = (
        stream_command(arguments, workspace=workspace)
        if workspace
        else subprocess.run(arguments, capture_output=True, text=True)
    )
    if completed.returncode == 255:
        raise NodeUnreachable(completed.stderr.strip() or completed.stdout.strip())
    if completed.returncode:
        raise ConfigError(completed.stderr.strip() or completed.stdout.strip())
    return completed


def workspace_run_state(
    node: dict[str, Any], workspace: dict[str, Any]
) -> dict[str, Any] | None:
    path = Path(workspace["path"])
    if is_local_target(node["target"]):
        if not path.exists():
            return None
        from .scheduler import state

        return state(path, persist=True)
    operating_system = node["capabilities"]["os"]
    path_type = PureWindowsPath if operating_system == "windows" else PurePosixPath
    run_path = str(path_type(workspace["path"]) / ".workspace" / "nk" / "run.json")
    command = remote_command(
        [
            "python" if operating_system == "windows" else "python3",
            "-c", REMOTE_STATE_PROBE, run_path,
        ],
        operating_system,
    )
    output = run_remote(node["target"], command).stdout.strip()
    if not output:
        return None
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid agent run state: {workspace['name']}@{node['name']}"
        ) from exc
    return value if isinstance(value, dict) else None


def preflight_runs(
    actions: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> set[str]:
    blocked: set[str] = set()
    for _, node, workspace in actions:
        reference = f"{workspace['name']}@{node['name']}"
        if node["name"] in blocked:
            continue
        try:
            value = workspace_run_state(node, workspace)
        except NodeUnreachable as exc:
            if node["state"] == "absent":
                continue
            blocked.add(node["name"])
            output.event("ERROR", exc, workspace=reference, error=True)
            continue
        except ConfigError as exc:
            blocked.add(node["name"])
            output.event("ERROR", exc, workspace=reference, error=True)
            continue
        if value and value.get("state") in {"reserved", "running", "interrupted"}:
            output.event(
                "ERROR", "unresolved agent run", workspace=reference, error=True
            )
            raise ConfigError(
                "unresolved agent run prevents cluster setup"
            )
    return blocked


def invoke_node_action(
    data: dict[str, Any], node: dict[str, Any], workspace: dict[str, Any], action: str,
    *, bootstrap_node: bool = False,
) -> None:
    identity = f"{workspace['name']}@{node['name']}"
    previous_owners = tuple(
        f"{candidate['name']}@{node['name']}"
        for candidate in node["workspaces"]
        if action == "create"
        and candidate["path"] == workspace["path"]
        and candidate["name"] != workspace["name"]
    )
    if is_local_target(node["target"]):
        if action == "create":
            materialize(
                data["name"], data["repository"], node["name"], workspace["name"],
                workspace["path"], node["capabilities"]["os"], previous_owners,
                identity, bootstrap_node,
            )
        elif action == "check-remove":
            validate_removal(
                data["name"], node["name"], workspace["name"], workspace["path"]
            )
        else:
            remove_materialized(
                data["name"], node["name"], workspace["name"], workspace["path"]
            )
        return
    arguments = [
        remote_executable(node["capabilities"]["os"]), "_node", action,
        "--cluster", data["name"], "--repository", data["repository"],
        "--node", node["name"], "--workspace", workspace["name"],
        "--path", workspace["path"], "--os", node["capabilities"]["os"],
    ]
    if action == "create":
        if bootstrap_node:
            arguments.append("--node-bootstrap")
        for previous_owner in previous_owners:
            arguments.extend(("--previous-owner", previous_owner))
    run_remote(
        node["target"], remote_command(arguments, node["capabilities"]["os"]),
        workspace=identity,
    )


def setup_cluster(data: dict[str, Any], *, node_name: str | None = None) -> int:
    from .scheduler import controller_lock

    nodes = data["nodes"]
    if node_name is not None:
        nodes = [find_node(data, node_name)]
    revision = application_revision()
    lock = home() / "clusters" / data["name"] / "state" / "controller.lock"
    actions = []
    for node in nodes:
        by_path: dict[str, dict[str, Any]] = {}
        for workspace in node["workspaces"]:
            if data["state"] == node["state"] == "present":
                current = by_path.get(workspace["path"])
                if current is not None and current["state"] == "present":
                    continue
                if workspace["state"] == "present":
                    by_path[workspace["path"]] = workspace
                    continue
            by_path[workspace["path"]] = workspace
        for workspace in by_path.values():
            present = data["state"] == node["state"] == workspace["state"] == "present"
            actions.append(("create" if present else "remove", node, workspace))
    output.configure(
        f"{workspace['name']}@{node['name']}" for _, node, workspace in actions
    )
    for action, node, workspace in actions:
        output.event(
            action.upper(), workspace["path"],
            workspace=f"{workspace['name']}@{node['name']}",
        )
    with controller_lock(lock):
        blocked = preflight_runs(actions)
        failures = len(blocked)
        decommissioned: set[str] = set()
        for node in nodes:
            if node["state"] != "absent" or node["name"] in blocked:
                continue
            removals = [
                (workspace, action)
                for action, candidate, workspace in actions
                if candidate is node and action == "remove"
            ]
            try:
                for workspace, _ in removals:
                    invoke_node_action(data, node, workspace, "check-remove")
            except NodeUnreachable:
                decommissioned.add(node["name"])
                output.event("DECOMMISSIONED", node["name"], "unreachable")
            except ConfigError as exc:
                blocked.add(node["name"])
                failures += 1
                output.event(
                    "ERROR", f"NODE@{node['name']}", exc, error=True
                )
        deployed: set[str] = set()
        for node in nodes:
            if (
                data["state"] != "present"
                or node["state"] != "present"
                or node["name"] in blocked
            ):
                continue
            try:
                if (
                    not is_local_target(node["target"])
                    and node["target"] not in deployed
                ):
                    deploy_remote(
                        node["target"], node["capabilities"]["os"], revision
                    )
                    deployed.add(node["target"])
            except (NodeUnreachable, ConfigError) as exc:
                blocked.add(node["name"])
                failures += 1
                output.event("ERROR", exc, workspace=f"NODE@{node['name']}", error=True)
        bootstrapped_nodes: set[str] = set()
        for action, node, workspace in actions:
            if node["name"] in blocked or node["name"] in decommissioned:
                continue
            reference = f"{workspace['name']}@{node['name']}"
            first_create = (
                action == "create" and node["name"] not in bootstrapped_nodes
            )
            try:
                if action == "remove" and node["state"] == "present":
                    invoke_node_action(data, node, workspace, "check-remove")
                invoke_node_action(
                    data, node, workspace, action,
                    bootstrap_node=first_create,
                )
                if first_create:
                    bootstrapped_nodes.add(node["name"])
            except NodeUnreachable as exc:
                failures += 1
                output.event("ERROR", exc, workspace=reference, error=True)
            except ConfigError as exc:
                failures += 1
                output.event("ERROR", exc, workspace=reference, error=True)
    return 1 if failures else 0


def command_works(arguments: list[str], *, cwd: Path | None = None) -> bool:
    try:
        return subprocess.run(
            arguments, cwd=cwd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except OSError:
        return False


def git_configured(field: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "config", "--get", field],
            cwd=Path.home(), capture_output=True, text=True,
        )
    except OSError:
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def inspect_node(repository: str, workspace_path: str | None = None) -> dict[str, Any]:
    operating_system = {
        "Linux": "linux", "Darwin": "macos", "Windows": "windows",
    }.get(platform.system())
    architecture = {
        "x86_64": "x86_64", "AMD64": "x86_64",
        "aarch64": "aarch64", "arm64": "aarch64",
    }.get(platform.machine())
    gpu_output = subprocess.run(
        ["nvidia-smi", "-L"], text=True, capture_output=True
    ) if shutil.which("nvidia-smi") else None
    gpus = (
        len([line for line in gpu_output.stdout.splitlines() if line.startswith("GPU ")])
        if gpu_output is not None and gpu_output.returncode == 0 else 0
    )
    result: dict[str, Any] = {
        "os": operating_system,
        "architecture": architecture,
        "gpu": gpus,
        "git": command_works(["git", "--version"]),
        "git_user_name": git_configured("user.name"),
        "git_user_email": git_configured("user.email"),
        "meta": shutil.which("meta") is not None,
        "harness": command_works(
            ["codex.cmd" if operating_system == "windows" else "codex", "--version"]
        ),
        "network": command_works(
            ["git", "ls-remote", repository, "HEAD"], cwd=Path.home()
        ),
    }
    if workspace_path is not None:
        root = Path(workspace_path)
        result["workspace_path"] = (root / ".git").exists()
        try:
            result["workspace_origin"] = (
                result["workspace_path"]
                and git(root, "remote", "get-url", "origin") == repository
            )
        except ConfigError:
            result["workspace_origin"] = False
        bootstrap = Path(__file__).resolve().parents[1] / "entrypoints" / "codex" / "codex_bootstrap"
        result["workspace_harness"] = (
            result["workspace_path"]
            and command_works([sys.executable, str(bootstrap), workspace_path])
        )
    return result


def remote_inspection(
    data: dict[str, Any], node: dict[str, Any], workspace_path: str | None = None
) -> dict[str, Any]:
    arguments = [
        remote_executable(node["capabilities"]["os"]), "_node", "inspect",
        "--repository", data["repository"],
    ]
    if workspace_path is not None:
        arguments.extend(("--path", workspace_path))
    output = run_remote(
        node["target"], remote_command(arguments, node["capabilities"]["os"])
    ).stdout
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid inspection from node {node['name']}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"invalid inspection from node {node['name']}")
    return value


def verify_cluster(data: dict[str, Any]) -> int:
    failures = 0
    output.configure(
        f"{workspace['name']}@{node['name']}"
        for node in data["nodes"] if node["state"] == "present"
        for workspace in node["workspaces"] if workspace["state"] == "present"
    )
    for node in data["nodes"]:
        if node["state"] != "present":
            continue
        local = is_local_target(node["target"])
        try:
            observed = (
                inspect_node(data["repository"])
                if local else remote_inspection(data, node)
            )
        except ConfigError:
            observed = {}
        checks = {
            "os": observed.get("os") == node["capabilities"]["os"],
            "architecture": observed.get("architecture") == node["capabilities"]["architecture"],
            "gpu": isinstance(observed.get("gpu"), int)
            and observed["gpu"] >= node["resources"]["gpu"],
            "git": observed.get("git") is True,
            "git_user_name": observed.get("git_user_name") is True,
            "git_user_email": observed.get("git_user_email") is True,
            "meta": observed.get("meta") is True,
            "harness": observed.get("harness") is True,
            "network": observed.get("network") is True,
        }
        for name, healthy in checks.items():
            output.event(
                "OK" if healthy else "ERROR", "NODE", node["name"], name,
                error=not healthy,
            )
            failures += not healthy
        for workspace in node["workspaces"]:
            if workspace["state"] != "present":
                continue
            try:
                workspace_state = (
                    inspect_node(data["repository"], workspace["path"])
                    if local else remote_inspection(data, node, workspace["path"])
                )
            except ConfigError:
                workspace_state = {}
            for name in ("workspace_path", "workspace_origin", "workspace_harness"):
                healthy = workspace_state.get(name) is True
                output.event(
                    "OK" if healthy else "ERROR", name,
                    workspace=f"{workspace['name']}@{node['name']}",
                    error=not healthy,
                )
                failures += not healthy
    return 1 if failures else 0


def internal(arguments: list[str]) -> int:
    import argparse

    output.configure(())
    parser = argparse.ArgumentParser(prog="nk _node")
    parser.add_argument(
        "action", choices=("create", "check-remove", "remove", "inspect")
    )
    parser.add_argument("--cluster")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--node")
    parser.add_argument("--workspace")
    parser.add_argument("--path")
    parser.add_argument("--os")
    parser.add_argument("--previous-owner", action="append", default=[])
    parser.add_argument("--node-bootstrap", action="store_true")
    args = parser.parse_args(arguments)
    try:
        if args.action == "inspect":
            print(json.dumps(inspect_node(args.repository, args.path), separators=(",", ":")))
            return 0
        if None in {args.cluster, args.node, args.workspace, args.path, args.os}:
            raise ConfigError(f"_node {args.action} requires workspace identity and OS")
        if args.action == "create":
            materialize(
                args.cluster, args.repository, args.node, args.workspace,
                args.path, args.os, tuple(args.previous_owner),
                bootstrap_node=args.node_bootstrap,
            )
        elif args.action == "check-remove":
            validate_removal(args.cluster, args.node, args.workspace, args.path)
        else:
            remove_materialized(args.cluster, args.node, args.workspace, args.path)
    except ConfigError as exc:
        print(f"ERROR\t{exc}", file=sys.stderr)
        return 1
    return 0
