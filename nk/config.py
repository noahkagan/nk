from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .transport import remote_prefix


class ConfigError(RuntimeError):
    pass


NAME_RE = re.compile(r"^[^@/\\\x00-\x1f\x7f]+$")


def home() -> Path:
    return Path(os.environ.get("NK_HOME", "~/.nk")).expanduser()


def validate_name(value: str, label: str) -> str:
    if value != value.strip() or value in {".", ".."} or NAME_RE.fullmatch(value) is None:
        raise ConfigError(f"invalid {label}: {value!r}")
    return value


def parse_workspace(value: str) -> tuple[str, str]:
    if value.count("@") != 1:
        raise ConfigError("workspace must use WORKSPACE@NODE")
    workspace, node = value.split("@")
    return validate_name(workspace, "workspace name"), validate_name(node, "node name")


def config_path(name: str) -> Path:
    return home() / "clusters" / validate_name(name, "cluster name") / "config.json"


def cordon_path(name: str) -> Path:
    return home() / "clusters" / validate_name(name, "cluster name") / "state" / "cordons.json"


def load_cordons(name: str) -> dict[str, str]:
    path = cordon_path(name)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid cordon state: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("invalid cordon state")
    for reference, reason in value.items():
        if not isinstance(reference, str) or not isinstance(reason, str) or not reason:
            raise ConfigError("invalid cordon state")
        parse_workspace(reference)
    return value


def save_cordons(name: str, value: dict[str, str]) -> None:
    path = cordon_path(name)
    if not value:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def set_cordon(name: str, reference: str, reason: str) -> None:
    parse_workspace(reference)
    if not reason:
        raise ConfigError("cordon reason must not be empty")
    value = load_cordons(name)
    value[reference] = reason
    save_cordons(name, value)


def clear_cordons(name: str, references: set[str]) -> None:
    value = load_cordons(name)
    changed = False
    for reference in references:
        changed = value.pop(reference, None) is not None or changed
    if changed:
        save_cordons(name, value)


def cluster_names() -> list[str]:
    root = home() / "clusters"
    if not root.exists():
        return []
    return sorted(
        path.parent.name for path in root.glob("*/config.json")
        if load(path)["state"] == "present"
    )


def select_cluster(name: str | None) -> str:
    if name is not None:
        return validate_name(name, "cluster name")
    names = cluster_names()
    if len(names) != 1:
        available = ", ".join(names) if names else "none"
        raise ConfigError(f"--cluster is required; available clusters: {available}")
    return names[0]


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"cluster does not exist: {path.parent.name}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid cluster configuration: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"name", "state", "repository", "nodes"}:
        raise ConfigError("invalid cluster configuration fields")
    validate_name(data.get("name", ""), "cluster name")
    if data["state"] not in {"present", "absent"}:
        raise ConfigError("invalid cluster state")
    if not isinstance(data["repository"], str) or not data["repository"]:
        raise ConfigError("invalid cluster repository")
    if not isinstance(data["nodes"], list):
        raise ConfigError("invalid cluster nodes")
    node_names: set[str] = set()
    for node in data["nodes"]:
        if not isinstance(node, dict):
            raise ConfigError("invalid cluster node")
        validate_node(node)
        if node["name"] in node_names:
            raise ConfigError(f"duplicate node name: {node['name']}")
        node_names.add(node["name"])
        workspace_names: set[str] = set()
        present_paths: set[str] = set()
        for workspace in node["workspaces"]:
            if not isinstance(workspace, dict) or set(workspace) != {"name", "state", "path"}:
                raise ConfigError("invalid workspace fields")
            validate_name(workspace["name"], "workspace name")
            if workspace["state"] not in {"present", "absent"}:
                raise ConfigError("invalid workspace state")
            validate_workspace_path(workspace["path"], node["capabilities"]["os"])
            if workspace["name"] in workspace_names:
                raise ConfigError(f"duplicate workspace name on node {node['name']}: {workspace['name']}")
            workspace_names.add(workspace["name"])
            if workspace["state"] == "present":
                if workspace["path"] in present_paths:
                    raise ConfigError(f"duplicate workspace path on node {node['name']}: {workspace['path']}")
                present_paths.add(workspace["path"])
    return data


def save(data: dict[str, Any]) -> None:
    path = config_path(data["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(encoded)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def get_cluster(name: str | None, *, present: bool = True) -> dict[str, Any]:
    selected = select_cluster(name)
    data = load(config_path(selected))
    if present and data["state"] != "present":
        raise ConfigError(f"cluster is absent: {selected}")
    return data


def find_node(data: dict[str, Any], name: str) -> dict[str, Any]:
    validate_name(name, "node name")
    matches = [node for node in data["nodes"] if node.get("name") == name]
    if len(matches) != 1:
        raise ConfigError(f"node does not exist: {name}")
    return matches[0]


def validate_node(node: dict[str, Any]) -> None:
    if set(node) != {"name", "state", "target", "capabilities", "resources", "workspaces"}:
        raise ConfigError("invalid node fields")
    validate_name(node["name"], "node name")
    if node["state"] not in {"present", "absent"}:
        raise ConfigError("invalid node state")
    if not isinstance(node["target"], str) or not node["target"]:
        raise ConfigError("invalid node target")
    try:
        remote_prefix(node["target"])
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if node["capabilities"].get("os") not in {"linux", "macos", "windows"}:
        raise ConfigError("node OS must be linux, macos, or windows")
    if set(node["capabilities"]) != {"os", "architecture"} or not isinstance(
        node["capabilities"]["architecture"], str
    ) or not node["capabilities"]["architecture"]:
        raise ConfigError("invalid node capabilities")
    if set(node["resources"]) != {"gpu"} or not isinstance(node["resources"]["gpu"], int) or node["resources"]["gpu"] < 0:
        raise ConfigError("invalid node resources")
    if not isinstance(node["workspaces"], list):
        raise ConfigError("invalid node workspaces")


def validate_workspace_path(value: str, operating_system: str) -> None:
    path = PureWindowsPath(value) if operating_system == "windows" else PurePosixPath(value)
    if not path.is_absolute():
        raise ConfigError("workspace path must be absolute for the node OS")


def find_workspace(node: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [workspace for workspace in node["workspaces"] if workspace.get("name") == name]
    if len(matches) != 1:
        raise ConfigError(f"workspace does not exist: {name}@{node['name']}")
    return matches[0]
