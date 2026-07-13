from __future__ import annotations

import json
import base64
import os
import signal
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest import mock

import pytest

from nk import cli, inventory, output, setup, task
from nk.config import ConfigError, get_cluster


@pytest.fixture(autouse=True)
def stable_application_revision(monkeypatch) -> None:
    monkeypatch.setattr(setup, "application_revision", lambda: "a" * 40)


def run(*arguments: str, cwd: Path | None = None) -> None:
    subprocess.run(arguments, cwd=cwd, check=True, capture_output=True)


def repository(tmp_path: Path) -> Path:
    bare = tmp_path / "workspace.git"
    seed = tmp_path / "seed"
    run("git", "init", "--bare", str(bare))
    run("git", "clone", str(bare), str(seed))
    run("git", "config", "user.name", "Test", cwd=seed)
    run("git", "config", "user.email", "test@example.invalid", cwd=seed)
    (seed / "TODO.md").write_text(
        "# TODO\n\n" + "".join(f"## {queue}\n\n" for queue in task.QUEUE_ORDER)
    )
    run("git", "add", "TODO.md", cwd=seed)
    run("git", "commit", "-m", "Initial", cwd=seed)
    run("git", "push", "origin", "HEAD", cwd=seed)
    return bare


def test_setup_materializes_and_marks_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    remote = repository(tmp_path)
    root = tmp_path / "author"
    inventory.cluster_add("local", str(remote))
    inventory.node_add("local", "node", "localhost", "linux", "x86_64", 0)
    inventory.workspace_add("local", "author@node", str(root))

    assert setup.setup_cluster(get_cluster("local")) == 0

    marker = json.loads((root / ".workspace/nk/ownership.json").read_text())
    assert marker == {
        "cluster": "local", "node": "node", "workspace": "author", "path": str(root)
    }
    assert setup.git(root, "status", "--porcelain=v1") == ""


def test_node_deployment_archive_contains_one_installed_product() -> None:
    archive = setup.deployment_archive()
    try:
        with tarfile.open(archive) as bundle:
            names = set(bundle.getnames())
            revision = bundle.extractfile("app/REVISION").read().decode("ascii")
        assert "app/nk/cli.py" in names
        assert "bin/nk" in names
        assert "bin/nk.cmd" in names
        assert "skills/task-coordination/SKILL.md" in names
        assert "skills/workstream-decoupling/SKILL.md" in names
        roots = ("app/nk", "app/entrypoints", "app/prompts", "bin", "skills")
        assert all(
            name == "app/REVISION"
            or any(name == root or name.startswith(root + "/") for root in roots)
            for name in names
        )
        assert "app/REVISION" in names
        assert revision == "a" * 40 + "\n"
        assert not any("__pycache__" in name for name in names)
    finally:
        archive.unlink()


def test_remote_deployment_transfers_only_product_archive(
    monkeypatch, tmp_path: Path
) -> None:
    archive = tmp_path / "nk-node.tar"
    archive.write_bytes(b"bundle")
    copied = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(setup, "deployment_archive", lambda revision: archive)
    monkeypatch.setattr(subprocess, "run", copied)
    monkeypatch.setattr(setup, "run_remote", mock.Mock())

    setup.deploy_remote("node", "linux", "a" * 40)

    assert copied.call_args_list == [
        mock.call(
            ["scp", str(archive), "node:.nk-install.tar"],
            capture_output=True,
            text=True,
        )
    ]
    command = setup.run_remote.call_args.args[1]
    assert "for discovery in ~/.agents/skills ~/.claude/skills" in command
    assert not archive.exists()


def test_cluster_setup_has_no_dry_run_option() -> None:
    with pytest.raises(SystemExit):
        cli.inventory_parser("cluster").parse_args(["setup", "--dry-run"])


def test_setup_can_target_one_node(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    remote = repository(tmp_path)
    roots = {name: tmp_path / name for name in ("author-a", "author-b")}
    inventory.cluster_add("local", str(remote))
    for name, root in roots.items():
        node = name.removeprefix("author-")
        inventory.node_add("local", node, "localhost", "linux", "x86_64", 0)
        inventory.workspace_add("local", f"{name}@{node}", str(root))

    assert setup.setup_cluster(get_cluster("local"), node_name="a") == 0

    assert roots["author-a"].is_dir()
    assert not roots["author-b"].exists()


def test_source_revision_requires_clean_committed_checkout(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run("git", "init", cwd=source)
    run("git", "config", "user.name", "Test", cwd=source)
    run("git", "config", "user.email", "test@example.invalid", cwd=source)
    (source / "file").write_text("clean\n")
    run("git", "add", "file", cwd=source)
    run("git", "commit", "-m", "Initial", cwd=source)

    revision = setup.source_revision(source)
    assert len(revision) == 40

    (source / "file").write_text("dirty\n")
    with pytest.raises(ConfigError, match="clean, committed"):
        setup.source_revision(source)


def test_remove_rejects_clean_unpublished_commit(tmp_path: Path) -> None:
    remote = repository(tmp_path)
    root = tmp_path / "author"
    setup.materialize("local", str(remote), "node", "author", str(root), "linux")
    run("git", "config", "user.name", "Test", cwd=root)
    run("git", "config", "user.email", "test@example.invalid", cwd=root)
    (root / "local.txt").write_text("unpublished\n")
    run("git", "add", "local.txt", cwd=root)
    run("git", "commit", "-m", "Local only", cwd=root)

    with pytest.raises(ConfigError, match="unpublished"):
        setup.remove_materialized("local", "node", "author", str(root))

    assert root.is_dir()


@pytest.mark.parametrize("ref_kind", ["branch", "tag"])
def test_remove_rejects_unpublished_ref_to_published_commit(
    tmp_path: Path, ref_kind: str
) -> None:
    remote = repository(tmp_path)
    root = tmp_path / "author"
    setup.materialize("local", str(remote), "node", "author", str(root), "linux")
    if ref_kind == "branch":
        run("git", "branch", "local-work", cwd=root)
    else:
        run("git", "tag", "local-work", cwd=root)

    with pytest.raises(ConfigError, match="unpublished ref"):
        setup.remove_materialized("local", "node", "author", str(root))

    assert root.is_dir()


def test_remove_deletes_clean_fully_pushed_workspace(tmp_path: Path) -> None:
    remote = repository(tmp_path)
    root = tmp_path / "author"
    setup.materialize("local", str(remote), "node", "author", str(root), "linux")

    setup.remove_materialized("local", "node", "author", str(root))

    assert not root.exists()


def test_remove_accepts_commit_published_only_by_tag(tmp_path: Path) -> None:
    remote = repository(tmp_path)
    root = tmp_path / "author"
    setup.materialize("local", str(remote), "node", "author", str(root), "linux")
    run("git", "config", "user.name", "Test", cwd=root)
    run("git", "config", "user.email", "test@example.invalid", cwd=root)
    default = task.resolve_default_branch(root).name
    run("git", "switch", "--detach", cwd=root)
    (root / "tagged.txt").write_text("published by tag\n")
    run("git", "add", "tagged.txt", cwd=root)
    run("git", "commit", "-m", "Tagged commit", cwd=root)
    run("git", "tag", "published-only-by-tag", cwd=root)
    run("git", "push", "origin", "published-only-by-tag", cwd=root)
    run("git", "switch", default, cwd=root)

    setup.remove_materialized("local", "node", "author", str(root))

    assert not root.exists()


def test_remove_ignores_unfetched_remote_only_tag(tmp_path: Path) -> None:
    remote = repository(tmp_path)
    root = tmp_path / "author"
    other = tmp_path / "other"
    setup.materialize("local", str(remote), "node", "author", str(root), "linux")
    run("git", "clone", str(remote), str(other))
    run("git", "config", "user.name", "Test", cwd=other)
    run("git", "config", "user.email", "test@example.invalid", cwd=other)
    run("git", "switch", "--orphan", "tag-only", cwd=other)
    (other / "orphan.txt").write_text("remote only\n")
    run("git", "add", "orphan.txt", cwd=other)
    run("git", "commit", "-m", "Remote tag only", cwd=other)
    run("git", "tag", "unfetched-remote-tag", cwd=other)
    run("git", "push", "origin", "unfetched-remote-tag", cwd=other)

    setup.remove_materialized("local", "node", "author", str(root))

    assert not root.exists()


def test_bootstrap_prefixes_workspace_output(tmp_path: Path, capsys) -> None:
    marker = tmp_path / "bootstrapped"
    script = tmp_path / "bootstrap.sh"
    script.write_text(
        f"#!/bin/sh\necho '[INFO] preparing'\necho '[OK  ] ready' >&2\ntouch {marker}\n"
    )
    script.chmod(0o755)
    output.configure(("author@node",))

    setup.bootstrap(tmp_path, "linux", "author@node")

    assert marker.exists()
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("\t", 1)[1] for line in lines] == [
        "author@node\t[INFO] preparing",
        "author@node\t[OK  ] ready",
    ]


def test_materialize_runs_node_bootstrap_before_workspace_bootstrap(
    monkeypatch, tmp_path: Path
) -> None:
    remote = repository(tmp_path)
    root = tmp_path / "author"
    calls: list[str] = []
    monkeypatch.setattr(
        setup, "node_bootstrap",
        lambda *args, **kwargs: calls.append("node"),
    )
    monkeypatch.setattr(
        setup, "bootstrap",
        lambda *args, **kwargs: calls.append("workspace"),
    )

    setup.materialize(
        "local", str(remote), "node", "author", str(root), "linux",
        bootstrap_node=True,
    )

    assert calls == ["node", "workspace"]


def test_materialize_syncs_existing_workspace_before_node_bootstrap(
    tmp_path: Path,
) -> None:
    remote = repository(tmp_path)
    root = tmp_path / "author"
    update = tmp_path / "update"
    setup.materialize("local", str(remote), "node", "author", str(root), "linux")
    run("git", "clone", str(remote), str(update))
    run("git", "config", "user.name", "Test", cwd=update)
    run("git", "config", "user.email", "test@example.invalid", cwd=update)
    script = update / "node-bootstrap.sh"
    script.write_text("#!/bin/sh\ntouch node-bootstrap-ran\n")
    script.chmod(0o755)
    run("git", "add", "node-bootstrap.sh", cwd=update)
    run("git", "commit", "-m", "Add node bootstrap", cwd=update)
    run("git", "push", "origin", "HEAD", cwd=update)

    setup.materialize(
        "local", str(remote), "node", "author", str(root), "linux",
        bootstrap_node=True,
    )

    assert (root / "node-bootstrap-ran").is_file()


def test_setup_runs_node_bootstrap_once_in_first_workspace(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    data = {
        "name": "local", "state": "present", "repository": "repo",
        "nodes": [{
            "name": "node", "state": "present", "target": "localhost",
            "capabilities": {"os": "linux", "architecture": "x86_64"},
            "resources": {"gpu": 0},
            "workspaces": [
                {"name": "author", "state": "present", "path": "/one"},
                {"name": "reviewer", "state": "present", "path": "/two"},
            ],
        }],
    }
    monkeypatch.setattr(setup, "workspace_run_state", lambda *args: None)
    calls: list[bool] = []

    def invoke(data, node, workspace, action, *, bootstrap_node=False):
        if action == "create":
            calls.append(bootstrap_node)

    monkeypatch.setattr(setup, "invoke_node_action", invoke)

    assert setup.setup_cluster(data) == 0
    assert calls == [True, False]


def test_first_workspace_failure_tries_next_node_bootstrap_seed(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    data = {
        "name": "local", "state": "present", "repository": "repo",
        "nodes": [{
            "name": "node", "state": "present", "target": "localhost",
            "capabilities": {"os": "linux", "architecture": "x86_64"},
            "resources": {"gpu": 0},
            "workspaces": [
                {"name": "author", "state": "present", "path": "/one"},
                {"name": "reviewer", "state": "present", "path": "/two"},
            ],
        }],
    }
    monkeypatch.setattr(setup, "workspace_run_state", lambda *args: None)
    calls: list[tuple[str, bool]] = []

    def invoke(data, node, workspace, action, *, bootstrap_node=False):
        if action == "create":
            calls.append((workspace["name"], bootstrap_node))
            if workspace["name"] == "author":
                raise ConfigError("workspace is dirty")

    monkeypatch.setattr(setup, "invoke_node_action", invoke)

    assert setup.setup_cluster(data) == 1
    assert calls == [("author", True), ("reviewer", True)]


def test_absent_node_preflights_all_workspaces_before_deletion(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    remote = repository(tmp_path)
    roots = [tmp_path / "author-1", tmp_path / "author-2"]
    inventory.cluster_add("local", str(remote))
    inventory.node_add("local", "node", "localhost", "linux", "x86_64", 0)
    for index, root in enumerate(roots, start=1):
        inventory.workspace_add("local", f"author-{index}@node", str(root))
        setup.materialize(
            "local", str(remote), "node", f"author-{index}", str(root), "linux"
        )
    (roots[1] / "untracked.txt").write_text("keep\n")
    inventory.node_remove("local", "node")

    assert setup.setup_cluster(get_cluster("local", present=False)) == 1
    assert all(root.exists() for root in roots)


def test_reused_workspace_path_supersedes_absent_record(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    remote = repository(tmp_path)
    root = tmp_path / "author"
    inventory.cluster_add("local", str(remote))
    inventory.node_add("local", "node", "localhost", "linux", "x86_64", 0)
    inventory.workspace_add("local", "old@node", str(root))
    assert setup.setup_cluster(get_cluster("local")) == 0
    inventory.workspace_remove("local", "old@node")
    assert setup.setup_cluster(get_cluster("local")) == 0
    inventory.workspace_add("local", "new@node", str(root))

    assert setup.setup_cluster(get_cluster("local")) == 0
    assert setup.setup_cluster(get_cluster("local")) == 0
    marker = json.loads((root / ".workspace/nk/ownership.json").read_text())
    assert marker["workspace"] == "new"
    inventory.node_remove("local", "node")
    assert setup.setup_cluster(get_cluster("local", present=False)) == 0
    assert not root.exists()


def test_reused_path_rejects_claim_owned_by_previous_identity(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    remote = repository(tmp_path)
    root = tmp_path / "author"
    inventory.cluster_add("local", str(remote))
    inventory.node_add("local", "node", "localhost", "linux", "x86_64", 0)
    inventory.workspace_add("local", "old@node", str(root))
    assert setup.setup_cluster(get_cluster("local")) == 0
    run("git", "config", "user.name", "Test", cwd=root)
    run("git", "config", "user.email", "test@example.invalid", cwd=root)
    task.create(root, "2026-07-04-claimed-before-reuse")
    manifest = root / "scratch/2026-07-04-claimed-before-reuse/task.json"
    manifest.write_text(
        json.dumps({"dependencies": [], "capabilities": {}, "resources": {}})
    )
    task.ready(root, "2026-07-04-claimed-before-reuse")
    monkeypatch.setenv("NK_WORKSPACE_OWNER", "old@node")
    task.claim(root, "2026-07-04-claimed-before-reuse")
    inventory.workspace_remove("local", "old@node")
    inventory.workspace_add("local", "new@node", str(root))

    assert setup.setup_cluster(get_cluster("local")) == 1
    marker = json.loads((root / ".workspace/nk/ownership.json").read_text())
    assert marker["workspace"] == "old"


def test_reused_path_rejects_previous_claim_when_checkout_marker_is_lost(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    remote = repository(tmp_path)
    root = tmp_path / "author"
    inventory.cluster_add("local", str(remote))
    inventory.node_add("local", "node", "localhost", "linux", "x86_64", 0)
    inventory.workspace_add("local", "old@node", str(root))
    assert setup.setup_cluster(get_cluster("local")) == 0
    run("git", "config", "user.name", "Test", cwd=root)
    run("git", "config", "user.email", "test@example.invalid", cwd=root)
    task.create(root, "2026-07-04-claimed-before-loss")
    manifest = root / "scratch/2026-07-04-claimed-before-loss/task.json"
    manifest.write_text(
        json.dumps({"dependencies": [], "capabilities": {}, "resources": {}})
    )
    task.ready(root, "2026-07-04-claimed-before-loss")
    monkeypatch.setenv("NK_WORKSPACE_OWNER", "old@node")
    task.claim(root, "2026-07-04-claimed-before-loss")
    inventory.workspace_remove("local", "old@node")
    shutil.rmtree(root)
    inventory.workspace_add("local", "new@node", str(root))

    assert setup.setup_cluster(get_cluster("local")) == 1
    assert not (root / setup.MARKER).exists()


def remote_absent_cluster() -> dict:
    return {
        "name": "remote",
        "state": "present",
        "repository": "repo",
        "nodes": [{
            "name": "node", "state": "absent", "target": "host",
            "capabilities": {"os": "linux", "architecture": "x86_64"},
            "resources": {"gpu": 0},
            "workspaces": [{
                "name": "author", "state": "absent", "path": "/work/author",
            }],
        }],
    }


def test_remote_removal_safety_rejection_is_failure(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    no_state = subprocess.CompletedProcess([], 0, "", "")
    rejected = ConfigError("ownership marker does not match")
    monkeypatch.setattr(setup, "run_remote", mock.Mock(side_effect=[no_state, rejected]))

    assert setup.setup_cluster(remote_absent_cluster()) == 1
    output = capsys.readouterr().err
    assert "ERROR\tNODE@node" in output
    assert "DECOMMISSIONED" not in output


def test_unreachable_absent_node_is_success(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    monkeypatch.setattr(
        setup, "run_remote", mock.Mock(side_effect=setup.NodeUnreachable("connection refused"))
    )

    assert setup.setup_cluster(remote_absent_cluster()) == 0
    assert "DECOMMISSIONED\tnode\tunreachable" in capsys.readouterr().out


def test_setup_refuses_active_run_before_materializing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    remote = repository(tmp_path)
    root = tmp_path / "author"
    inventory.cluster_add("local", str(remote))
    inventory.node_add("local", "node", "localhost", "linux", "x86_64", 0)
    inventory.workspace_add("local", "author@node", str(root))
    setup.materialize("local", str(remote), "node", "author", str(root), "linux")
    run_path = root / ".workspace/nk/run.json"
    run_path.write_text(json.dumps({"state": "running", "pid": os.getpid()}))

    with mock.patch.object(setup, "materialize") as materialize:
        with pytest.raises(ConfigError, match="unresolved agent run"):
            setup.setup_cluster(get_cluster("local"))
    materialize.assert_not_called()


def test_setup_refuses_interrupted_run_before_materializing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        setup, "workspace_run_state",
        lambda *args: {"state": "interrupted", "slug": "task"},
    )

    with pytest.raises(ConfigError, match="unresolved agent run"):
        setup.preflight_runs([(
            "update",
            {"name": "node", "state": "present"},
            {"name": "author"},
        )])


def test_remote_state_probe_requires_the_supervisor_pid(tmp_path) -> None:
    if os.name != "posix":
        return
    supervisor = subprocess.Popen(
        ["sh", "-c", "sleep 30 &"], start_new_session=True
    )
    pid = supervisor.pid
    supervisor.wait(timeout=5)
    run_path = tmp_path / "run.json"
    run_path.write_text(json.dumps({"state": "running", "pid": pid}))
    try:
        completed = subprocess.run(
            [sys.executable, "-c", setup.REMOTE_STATE_PROBE, str(run_path)],
            check=True, capture_output=True, text=True,
        )
    finally:
        os.killpg(pid, signal.SIGTERM)

    assert json.loads(completed.stdout)["state"] == "interrupted"


def test_unreachable_node_does_not_block_independent_local_setup(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))
    remote = repository(tmp_path)
    root = tmp_path / "author"
    data = {
        "name": "mixed",
        "state": "present",
        "repository": str(remote),
        "nodes": [
            {
                "name": "offline",
                "state": "present",
                "target": "offline",
                "capabilities": {"os": "linux", "architecture": "x86_64"},
                "resources": {"gpu": 0},
                "workspaces": [
                    {
                        "name": "remote-author",
                        "state": "present",
                        "path": "/work/author",
                    }
                ],
            },
            {
                "name": "local",
                "state": "present",
                "target": "localhost",
                "capabilities": {"os": "linux", "architecture": "x86_64"},
                "resources": {"gpu": 0},
                "workspaces": [
                    {
                        "name": "author",
                        "state": "present",
                        "path": str(root),
                    }
                ],
            },
        ],
    }
    original = setup.workspace_run_state

    def run_state(node, workspace):
        if node["name"] == "offline":
            raise setup.NodeUnreachable("connection refused")
        return original(node, workspace)

    monkeypatch.setattr(setup, "workspace_run_state", run_state)

    assert setup.setup_cluster(data) == 1
    assert root.is_dir()
    assert "remote-author@offline\tERROR" in capsys.readouterr().err


def test_windows_remote_run_preflight_uses_native_path(monkeypatch) -> None:
    node = {
        "name": "win", "state": "present", "target": "host",
        "capabilities": {"os": "windows", "architecture": "x86_64"},
    }
    workspace = {"name": "author", "path": r"C:\work\author"}
    completed = subprocess.CompletedProcess([], 0, "", "")
    run_process = mock.Mock(return_value=completed)
    monkeypatch.setattr(subprocess, "run", run_process)

    assert setup.workspace_run_state(node, workspace) is None

    command = run_process.call_args.args[0][2]
    assert command.startswith("powershell ")
    script = base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16le")
    assert "'python' '-c'" in script
    assert r'path + \".tmp-\"' in script
    assert r".nk\bin\nk.cmd" not in script
    assert r"C:\work\author\.workspace\nk\run.json" in script
    assert "C:/" not in script


def test_windows_remote_command_quotes_cmd_metacharacters() -> None:
    command = setup.remote_command(
        [
            r".nk\bin\nk.cmd", "_node", "create", "--workspace",
            "author&reviewer%USERNAME%",
        ],
        "windows",
    )

    assert command.startswith("powershell ")
    script = base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16le")
    assert "'author&reviewer%USERNAME%'" in script
    assert "%" not in command


def test_remote_remove_keeps_previous_helper_protocol(monkeypatch) -> None:
    node = remote_absent_cluster()["nodes"][0]
    workspace = node["workspaces"][0]
    commands = []
    monkeypatch.setattr(
        setup,
        "run_remote",
        lambda target, command, **kwargs: commands.append((command, kwargs)),
    )

    setup.invoke_node_action(
        remote_absent_cluster(), node, workspace, "check-remove"
    )

    assert "--role" not in commands[0][0]


def test_remote_workspace_action_streams_output(monkeypatch) -> None:
    data = remote_absent_cluster()
    node = data["nodes"][0]
    node["state"] = "present"
    workspace = node["workspaces"][0]
    workspace["state"] = "present"
    calls = []

    monkeypatch.setattr(
        setup,
        "run_remote",
        lambda target, command, **kwargs: calls.append(kwargs),
    )

    setup.invoke_node_action(data, node, workspace, "create")

    assert calls == [{"workspace": f"{workspace['name']}@{node['name']}"}]


def test_remote_verify_uses_deployed_nk_and_remote_workspace_path(
    monkeypatch, capsys
) -> None:
    data = {
        "repository": "git@example.test:workspace.git",
        "nodes": [
            {
                "name": "remote",
                "state": "present",
                "target": "host",
                "capabilities": {"os": "linux", "architecture": "x86_64"},
                "resources": {"gpu": 0},
                "workspaces": [
                    {
                        "name": "author",
                        "state": "present",
                        "path": "/remote/workspace",
                    }
                ],
            }
        ],
    }
    calls = []

    def remote(target, command):
        calls.append(command)
        value = {
            "os": "linux",
            "architecture": "x86_64",
            "gpu": 0,
            "git": True,
            "git_user_name": True,
            "git_user_email": True,
            "meta": True,
            "harness": True,
            "network": True,
        }
        if "--path" in command:
            value.update({
                "workspace_path": True,
                "workspace_origin": True,
                "workspace_harness": True,
            })
        output = json.dumps(value)
        return subprocess.CompletedProcess([], 0, output, "")

    monkeypatch.setattr(setup, "run_remote", remote)

    assert setup.verify_cluster(data) == 0
    assert calls[0].startswith(".nk/bin/nk _node inspect")
    assert "/remote/workspace" in calls[1]
    assert "author@remote\tOK\tworkspace_origin" in capsys.readouterr().out


def test_verify_accepts_more_physical_gpus_than_declared(monkeypatch) -> None:
    data = {
        "repository": "repo",
        "nodes": [{
            "name": "local", "state": "present", "target": "localhost",
            "capabilities": {"os": "linux", "architecture": "x86_64"},
            "resources": {"gpu": 1},
            "workspaces": [],
        }],
    }
    monkeypatch.setattr(
        setup,
        "inspect_node",
        lambda repository: {
            "os": "linux", "architecture": "x86_64", "gpu": 2,
            "git": True, "meta": True, "harness": True, "network": True,
            "git_user_name": True, "git_user_email": True,
        },
    )

    assert setup.verify_cluster(data) == 0


def test_node_network_probe_ignores_current_repository_config(monkeypatch) -> None:
    calls = []

    def command_works(arguments, *, cwd=None):
        calls.append((arguments, cwd))
        return True

    monkeypatch.setattr(setup, "command_works", command_works)
    monkeypatch.setattr(setup.shutil, "which", lambda command: None)

    observed = setup.inspect_node("git@example.test:workspace.git")

    assert observed["network"] is True
    assert (
        ["git", "ls-remote", "git@example.test:workspace.git", "HEAD"],
        Path.home(),
    ) in calls


@pytest.mark.parametrize("field", ["user.name", "user.email"])
def test_git_configured_requires_nonempty_effective_value(
    monkeypatch, field: str
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        mock.Mock(return_value=subprocess.CompletedProcess([], 0, "  \n", "")),
    )

    assert setup.git_configured(field) is False
    subprocess.run.assert_called_once_with(
        ["git", "config", "--get", field],
        cwd=Path.home(), capture_output=True, text=True,
    )


@pytest.mark.parametrize("missing", ["git_user_name", "git_user_email"])
def test_verify_requires_user_local_git_identity(
    monkeypatch, capsys, missing: str
) -> None:
    data = {
        "repository": "repo",
        "nodes": [{
            "name": "local", "state": "present", "target": "localhost",
            "capabilities": {"os": "linux", "architecture": "x86_64"},
            "resources": {"gpu": 0}, "workspaces": [],
        }],
    }
    observed = {
        "os": "linux", "architecture": "x86_64", "gpu": 0,
        "git": True, "git_user_name": True, "git_user_email": True,
        "meta": True, "harness": True, "network": True,
    }
    observed[missing] = False
    monkeypatch.setattr(setup, "inspect_node", lambda repository: observed)

    assert setup.verify_cluster(data) == 1
    assert f"ERROR\tNODE\tlocal\t{missing}" in capsys.readouterr().err


def test_verify_reports_malformed_workspace_and_continues(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    for root in (good, bad):
        run("git", "init", str(root))
    run("git", "remote", "add", "origin", "repo", cwd=good)
    data = {
        "repository": "repo",
        "nodes": [
            {
                "name": "local",
                "state": "present",
                "target": "localhost",
                "capabilities": {"os": "linux", "architecture": "x86_64"},
                "resources": {"gpu": 0},
                "workspaces": [
                    {"name": "bad", "state": "present", "path": str(bad)},
                    {"name": "good", "state": "present", "path": str(good)},
                ],
            }
        ],
    }
    monkeypatch.setattr(
        setup,
        "inspect_node",
        lambda repository, path=None: {
            "os": "linux", "architecture": "x86_64", "gpu": 0,
            "git": True, "meta": True, "harness": True, "network": True,
            "git_user_name": True, "git_user_email": True,
            **(
                {
                    "workspace_path": True,
                    "workspace_origin": path == str(good),
                    "workspace_harness": True,
                }
                if path is not None else {}
            ),
        },
    )

    assert setup.verify_cluster(data) == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "bad@local \tERROR\tworkspace_origin" in output
    assert "good@local\tOK\tworkspace_origin" in output
