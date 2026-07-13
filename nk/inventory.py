from __future__ import annotations

from typing import Any

from .config import (
    ConfigError,
    clear_cordons,
    config_path,
    find_node,
    find_workspace,
    get_cluster,
    load,
    parse_workspace,
    save,
    set_cordon,
    validate_name,
    validate_node,
    validate_workspace_path,
)


def cluster_add(name: str, repository: str) -> None:
    validate_name(name, "cluster name")
    path = config_path(name)
    if path.exists():
        data = load(path)
        if data["state"] == "present":
            raise ConfigError(f"cluster already exists: {name}")
        if data["repository"] != repository:
            raise ConfigError("an absent cluster can only be restored with the same repository")
        data["state"] = "present"
    else:
        if not repository:
            raise ConfigError("repository must not be empty")
        data = {"name": name, "state": "present", "repository": repository, "nodes": []}
    save(data)
    print(f"CLUSTER\t{name}\tpresent")


def cluster_remove(name: str) -> None:
    data = get_cluster(name)
    references = {
        f"{workspace['name']}@{node['name']}"
        for node in data["nodes"] for workspace in node["workspaces"]
    }
    data["state"] = "absent"
    for node in data["nodes"]:
        node["state"] = "absent"
        for workspace in node["workspaces"]:
            workspace["state"] = "absent"
    clear_cordons(name, references)
    save(data)
    print(f"CLUSTER\t{name}\tabsent")


def cluster_uncordon(name: str | None) -> None:
    data = get_cluster(name)
    references = {
        f"{workspace['name']}@{node['name']}"
        for node in data["nodes"] for workspace in node["workspaces"]
    }
    clear_cordons(data["name"], references)
    print(f"CLUSTER\t{data['name']}\tuncordoned")


def node_add(
    cluster: str | None,
    name: str,
    target: str,
    operating_system: str,
    architecture: str,
    gpus: int,
) -> None:
    data = get_cluster(cluster)
    validate_name(name, "node name")
    if operating_system not in {"linux", "macos", "windows"}:
        raise ConfigError("node OS must be linux, macos, or windows")
    if architecture not in {"x86_64", "aarch64"}:
        raise ConfigError("node architecture must be x86_64 or aarch64")
    if gpus < 0:
        raise ConfigError("GPU count must be nonnegative")
    matches = [node for node in data["nodes"] if node.get("name") == name]
    if matches and matches[0]["state"] == "present":
        raise ConfigError(f"node already exists: {name}")
    workspaces = matches[0]["workspaces"] if matches else []
    replacement: dict[str, Any] = {
        "name": name,
        "state": "present",
        "target": target,
        "capabilities": {"os": operating_system, "architecture": architecture},
        "resources": {"gpu": gpus},
        "workspaces": workspaces,
    }
    validate_node(replacement)
    if matches:
        data["nodes"][data["nodes"].index(matches[0])] = replacement
    else:
        data["nodes"].append(replacement)
    save(data)
    print(f"NODE\t{name}\tpresent")


def node_remove(cluster: str | None, name: str) -> None:
    data = get_cluster(cluster)
    node = find_node(data, name)
    if node["state"] != "present":
        raise ConfigError(f"node is already absent: {name}")
    clear_cordons(
        data["name"],
        {f"{workspace['name']}@{name}" for workspace in node["workspaces"]},
    )
    node["state"] = "absent"
    for workspace in node["workspaces"]:
        workspace["state"] = "absent"
    save(data)
    print(f"NODE\t{name}\tabsent")


def workspace_add(cluster: str | None, reference: str, path: str) -> None:
    data = get_cluster(cluster)
    workspace_name, node_name = parse_workspace(reference)
    node = find_node(data, node_name)
    if node["state"] != "present":
        raise ConfigError(f"node is absent: {node_name}")
    validate_workspace_path(path, node["capabilities"]["os"])
    if any(
        workspace["state"] == "present" and workspace["path"] == path
        and workspace["name"] != workspace_name
        for workspace in node["workspaces"]
    ):
        raise ConfigError(f"workspace path is already in use on node {node_name}: {path}")
    matches = [workspace for workspace in node["workspaces"] if workspace.get("name") == workspace_name]
    if matches and matches[0]["state"] == "present":
        raise ConfigError(f"workspace already exists: {reference}")
    replacement = {"name": workspace_name, "state": "present", "path": path}
    if matches:
        node["workspaces"].remove(matches[0])
    node["workspaces"].append(replacement)
    clear_cordons(data["name"], {reference})
    save(data)
    print(f"WORKSPACE\t{reference}\tpresent")


def workspace_remove(cluster: str | None, reference: str) -> None:
    data = get_cluster(cluster)
    workspace_name, node_name = parse_workspace(reference)
    node = find_node(data, node_name)
    workspace = find_workspace(node, workspace_name)
    if workspace["state"] != "present":
        raise ConfigError(f"workspace is already absent: {reference}")
    workspace["state"] = "absent"
    clear_cordons(data["name"], {reference})
    save(data)
    print(f"WORKSPACE\t{reference}\tabsent")


def workspace_cordon(cluster: str | None, reference: str) -> None:
    data = get_cluster(cluster)
    workspace_name, node_name = parse_workspace(reference)
    node = find_node(data, node_name)
    workspace = find_workspace(node, workspace_name)
    if node["state"] != "present" or workspace["state"] != "present":
        raise ConfigError(f"workspace is absent: {reference}")
    set_cordon(data["name"], reference, "cordoned by operator")
    print(f"WORKSPACE\t{reference}\tcordoned")


def workspace_uncordon(cluster: str | None, reference: str) -> None:
    data = get_cluster(cluster)
    workspace_name, node_name = parse_workspace(reference)
    node = find_node(data, node_name)
    workspace = find_workspace(node, workspace_name)
    if node["state"] != "present" or workspace["state"] != "present":
        raise ConfigError(f"workspace is absent: {reference}")
    clear_cordons(data["name"], {reference})
    print(f"WORKSPACE\t{reference}\tuncordoned")
