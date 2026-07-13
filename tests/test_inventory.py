from __future__ import annotations

import json

import pytest

from nk import inventory
from nk.config import ConfigError, config_path, load_cordons, parse_workspace


def test_inventory_edits_desired_state_without_materializing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "home"))

    inventory.cluster_add("local", "git@example.invalid:owner/workspace.git")
    inventory.node_add("local", "linux_node", "localhost", "linux", "x86_64", 2)
    inventory.workspace_add("local", "author@linux_node", "/work/author")

    data = json.loads(config_path("local").read_text())
    assert data["nodes"][0]["workspaces"] == [
        {"name": "author", "state": "present", "path": "/work/author"}
    ]
    assert not (tmp_path / "home" / "clusters" / "local" / "state").exists()


def test_removing_node_marks_descendants_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    inventory.cluster_add("local", "repo")
    inventory.node_add("local", "node", "localhost", "linux", "aarch64", 0)
    inventory.workspace_add("local", "same@node", "/one")

    inventory.node_remove("local", "node")

    data = json.loads(config_path("local").read_text())
    assert data["nodes"][0]["state"] == "absent"
    assert data["nodes"][0]["workspaces"][0]["state"] == "absent"


def test_workspace_names_are_node_scoped(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    inventory.cluster_add("local", "repo")
    for name in ("one", "two"):
        inventory.node_add("local", name, name, "linux", "x86_64", 0)
        inventory.workspace_add("local", f"author@{name}", f"/{name}")

    assert parse_workspace("author@one") == ("author", "one")


def test_node_target_rejects_malformed_shell_words(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    inventory.cluster_add("local", "repo")

    with pytest.raises(ConfigError, match="No closing quotation"):
        inventory.node_add("local", "bad", "ssh 'unterminated", "linux", "x86_64", 0)


@pytest.mark.parametrize("value", ["author", "a@b@c", "../a@node", "a@.."])
def test_workspace_reference_rejects_structural_ambiguity(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_workspace(value)


def test_windows_workspace_requires_native_absolute_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    inventory.cluster_add("local", "repo")
    inventory.node_add("local", "win", "host", "windows", "x86_64", 0)

    with pytest.raises(ConfigError, match="absolute"):
        inventory.workspace_add("local", "author@win", "/posix/path")
    inventory.workspace_add("local", "author@win", r"C:\work\author")


def test_workspace_cordon_lifecycle_is_persistent_and_idempotent(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    inventory.cluster_add("local", "repo")
    inventory.node_add("local", "node", "localhost", "linux", "x86_64", 0)
    inventory.workspace_add("local", "author@node", "/work")

    inventory.workspace_cordon("local", "author@node")
    inventory.workspace_cordon("local", "author@node")
    assert load_cordons("local") == {"author@node": "cordoned by operator"}

    inventory.workspace_uncordon("local", "author@node")
    inventory.workspace_uncordon("local", "author@node")
    assert load_cordons("local") == {}


def test_cluster_uncordon_clears_every_workspace(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    inventory.cluster_add("local", "repo")
    inventory.node_add("local", "node", "localhost", "linux", "x86_64", 0)
    for name in ("author", "author-2"):
        inventory.workspace_add("local", f"{name}@node", f"/{name}")
        inventory.workspace_cordon("local", f"{name}@node")

    inventory.cluster_uncordon("local")
    inventory.cluster_uncordon("local")

    assert load_cordons("local") == {}


def test_removing_and_readding_workspace_discards_cordon(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    inventory.cluster_add("local", "repo")
    inventory.node_add("local", "node", "localhost", "linux", "x86_64", 0)
    inventory.workspace_add("local", "author@node", "/work")
    inventory.workspace_cordon("local", "author@node")

    inventory.workspace_remove("local", "author@node")
    assert load_cordons("local") == {}
    inventory.workspace_add("local", "author@node", "/work")
    assert load_cordons("local") == {}
