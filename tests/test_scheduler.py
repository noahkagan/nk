from __future__ import annotations

import argparse
import asyncio
import io
import inspect
import json
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from nk import scheduler
from nk.config import load_cordons, set_cordon


@pytest.fixture(autouse=True)
def isolated_nk_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path / "nk-home"))


def workspace(name: str, gpu: int = 1) -> scheduler.Workspace:
    return scheduler.Workspace(
        reference=f"{name}@node",
        path=f"/work/{name}",
        node="node",
        target="localhost",
        capabilities={"os": "linux", "architecture": "x86_64"},
        resources={"gpu": gpu},
    )


def cluster() -> dict:
    return {
        "name": "local",
        "state": "present",
        "repository": "repo",
        "nodes": [{
            "name": "node",
            "state": "present",
            "target": "localhost",
            "capabilities": {"os": "linux", "architecture": "x86_64"},
            "resources": {"gpu": 2},
            "workspaces": [
                {"name": "author-1", "state": "present", "path": "/one"},
                {"name": "author-2", "state": "present", "path": "/two"},
            ],
        }],
    }


def snapshot(candidates: list[dict], occupied: list[str] | None = None) -> dict:
    return {"candidates": candidates, "occupied": occupied or []}


def test_queue_snapshot_defers_claim_until_resume_after(tmp_path: Path) -> None:
    slug = "2026-07-12-waiting-for-external-result"
    (tmp_path / "scratch" / slug).mkdir(parents=True)
    (tmp_path / "scratch" / slug / "README.md").write_text(
        "# Wait for External Result\n"
    )
    queues = "\n".join(
        f"## {name}\n\n" + (
            f"- [`{slug}`](scratch/{slug}/README.md)\n" if name == "Authoring" else ""
        )
        for name in scheduler.task.QUEUE_ORDER
    )
    (tmp_path / "TODO.md").write_text(f"# Tasks\n\n{queues}")
    (tmp_path / "scratch" / slug / "claim.json").write_text(json.dumps({
        "owner": "author@node",
        "claim_id": "a" * 32,
        "spec_sha": "b" * 40,
        "repositories": [],
        "resume_after": "2030-01-02T03:04:05Z",
    }))

    with (
        mock.patch.object(scheduler.task, "resolve_default_branch"),
        mock.patch.object(scheduler.task, "fetch_ref", return_value="tree"),
        mock.patch.object(scheduler.task, "synchronize_checkout"),
        mock.patch.object(scheduler.task, "dependencies_from_checkout", return_value={}),
    ):
        result = scheduler.queue_snapshot(tmp_path)

    assert result == {"candidates": [], "occupied": ["author@node"]}


def observations(read):
    async def observe(items):
        results = []
        for item in items:
            try:
                if isinstance(read, BaseException):
                    raise read
                value = read(item) if callable(read) else read
                if inspect.isawaitable(value):
                    value = await value
            except BaseException as exc:
                value = exc
            results.append(value)
        return results

    return observe


def test_fit_uses_declared_capabilities_and_free_resources() -> None:
    candidate = {
        "capabilities": {"os": "linux", "architecture": "x86_64"},
        "resources": {"gpu": 1},
    }
    assert scheduler.fits(candidate, workspace("author"), 1)
    assert not scheduler.fits(candidate, workspace("author"), 0)
    assert not scheduler.fits(
        {**candidate, "capabilities": {"os": "windows"}}, workspace("author"), 1
    )


def test_schedule_reserves_resources_before_launching() -> None:
    events: list[tuple[str, str]] = []
    candidates = [
        {"slug": "first", "capabilities": {}, "resources": {"gpu": 1}},
        {"slug": "second", "capabilities": {}, "resources": {"gpu": 1}},
    ]
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(scheduler, "read_queue", return_value=snapshot(candidates)),
        mock.patch.object(scheduler, "reserve_state", side_effect=lambda item, slug, gpu: events.append(("reserve", slug))),
        mock.patch.object(scheduler, "launch", side_effect=lambda item, slug: events.append(("launch", slug))),
    ):
        assert scheduler.schedule_once(cluster()) == 2

    assert events == [
        ("reserve", "first"), ("launch", "first"),
        ("reserve", "second"), ("launch", "second"),
    ]


def test_schedule_preserves_specific_platform_capacity() -> None:
    data = cluster()
    data["nodes"] = [
        {
            "name": "a-windows",
            "state": "present",
            "target": "windows.example",
            "capabilities": {"os": "windows", "architecture": "x86_64"},
            "resources": {"gpu": 0},
            "workspaces": [{
                "name": "author", "state": "present", "path": "C:\\work\\author",
            }],
        },
        {
            "name": "z-linux",
            "state": "present",
            "target": "linux.example",
            "capabilities": {"os": "linux", "architecture": "x86_64"},
            "resources": {"gpu": 0},
            "workspaces": [{
                "name": "author", "state": "present", "path": "/work/author",
            }],
        },
    ]
    candidates = [
        {"slug": "portable", "capabilities": {}, "resources": {"gpu": 0}},
        {
            "slug": "windows-only",
            "capabilities": {"os": "windows"}, "resources": {"gpu": 0},
        },
    ]
    launched: list[tuple[str, str]] = []
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(scheduler, "read_queue", return_value=snapshot(candidates)),
        mock.patch.object(scheduler, "reserve_state"),
        mock.patch.object(
            scheduler,
            "launch",
            side_effect=lambda item, slug: launched.append((item.reference, slug)),
        ),
    ):
        assert scheduler.schedule_once(data) == 2

    assert launched == [
        ("author@z-linux", "portable"),
        ("author@a-windows", "windows-only"),
    ]


def test_node_state_checks_run_concurrently() -> None:
    data = cluster()
    data["nodes"].append({
        "name": "other",
        "state": "present",
        "target": "other@example",
        "capabilities": {"os": "linux", "architecture": "x86_64"},
        "resources": {"gpu": 0},
        "workspaces": [{
            "name": "reviewer", "state": "present", "path": "/review",
        }],
    })
    checked = 0
    checks_started = asyncio.Event()

    async def check(items):
        nonlocal checked
        checked += 1
        if checked == 2:
            checks_started.set()
        await asyncio.wait_for(checks_started.wait(), timeout=1)
        return [None] * len(items)

    with (
        mock.patch.object(scheduler, "observe_node_states", side_effect=check),
        mock.patch.object(scheduler, "read_queue", return_value=snapshot([])),
    ):
        assert scheduler.schedule_once(data) == 0


def test_queue_is_read_once_from_local_workspace() -> None:
    data = cluster()
    data["nodes"][0]["target"] = "host@example"
    data["nodes"].append({
        "name": "local-node",
        "state": "present",
        "target": "localhost",
        "capabilities": {"os": "linux", "architecture": "x86_64"},
        "resources": {"gpu": 0},
        "workspaces": [{
            "name": "local-author", "state": "present", "path": "/local",
        }],
    })
    local = scheduler.workspaces(data)[-1]
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([])
        ) as read_queue,
    ):
        assert scheduler.schedule_once(data) == 0

    read_queue.assert_awaited_once_with(local)


def test_queue_reader_falls_back_after_failure() -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler,
            "read_queue",
            side_effect=[scheduler.TransportFailure("offline"), snapshot([])],
        ) as read_queue,
    ):
        assert scheduler.schedule_once(data) == 0

    assert read_queue.await_args_list == [mock.call(first), mock.call(second)]


def test_queue_reader_falls_back_after_queue_failure() -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler,
            "read_queue",
            side_effect=[scheduler.ConfigError("invalid queue"), snapshot([])],
        ) as read_queue,
    ):
        assert scheduler.schedule_once(data) == 0

    assert read_queue.await_args_list == [mock.call(first), mock.call(second)]


def test_queue_reader_checkout_fault_is_cordoned(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    data = cluster()
    first, second = scheduler.workspaces(data)
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler,
            "read_queue",
            side_effect=[scheduler.WorkspaceFault("queue checkout is dirty"), snapshot([])],
        ),
    ):
        assert scheduler.schedule_once(data) == 0

    assert load_cordons("local") == {
        first.reference: "queue checkout is dirty"
    }


def test_queue_reader_election_and_idle_use_reader_identity() -> None:
    data = cluster()
    selected = scheduler.workspaces(data)[0]
    events = []
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(scheduler, "read_queue", return_value=snapshot([])),
        mock.patch.object(
            scheduler.output, "event",
            side_effect=lambda name, *details, **fields: events.append(
                (name, details, fields)
            ),
        ),
    ):
        assert scheduler.schedule_once(data) == 0

    assert (
        "ELECTED", ("task queue reader",),
        {"workspace": selected.reference, "verbose": True},
    ) in events
    assert (
        "READING", ("task queue",),
        {"workspace": selected.reference, "verbose": True},
    ) in events
    assert events[-1] == (
        "IDLE", ("no task launched",),
        {"workspace": selected.reference, "verbose": True},
    )


def test_cancelled_observation_terminates_subprocess() -> None:
    class Process:
        returncode = None

        def __init__(self):
            self.started = asyncio.Event()
            self.terminated = False

        async def communicate(self):
            self.started.set()
            await asyncio.Future()

        def terminate(self):
            self.terminated = True

        async def wait(self):
            self.returncode = -1
            return self.returncode

    process = Process()

    async def cancel():
        with mock.patch.object(
            asyncio, "create_subprocess_exec", return_value=process
        ):
            task = asyncio.create_task(
                scheduler.command_output_async(workspace("author"), ["probe"])
            )
            await process.started.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(cancel())

    assert process.terminated


def test_slow_observation_times_out_and_terminates_subprocess() -> None:
    class Process:
        returncode = None

        def __init__(self):
            self.terminated = False

        async def communicate(self):
            await asyncio.Future()

        def terminate(self):
            self.terminated = True

        async def wait(self):
            self.returncode = -1
            return self.returncode

    process = Process()

    async def observe():
        with (
            mock.patch.object(
                asyncio, "create_subprocess_exec", return_value=process
            ),
            mock.patch.object(scheduler, "REMOTE_OBSERVATION_TIMEOUT_S", 0.01),
        ):
            with pytest.raises(
                scheduler.TransportFailure, match="remote observation timed out"
            ):
                await scheduler.command_output_async(
                    workspace("author"), ["probe"]
                )

    asyncio.run(observe())

    assert process.terminated


def test_node_observation_uses_one_command_for_all_workspaces() -> None:
    first = workspace("author-1")
    second = workspace("author-2")
    response = [
        {"reference": first.reference, "state": None},
        {"reference": second.reference, "state": {"state": "running"}},
    ]
    with mock.patch.object(
        scheduler, "command_output_async", return_value=json.dumps(response)
    ) as command:
        results = asyncio.run(scheduler.observe_node_states([first, second]))

    assert results == [None, {"state": "running"}]
    command.assert_awaited_once()
    probe, arguments = command.await_args.args
    assert probe.reference == "NODE@node"
    assert arguments == [
        scheduler.node_nk(first), "scheduler", "_states",
        "--workspace", first.reference, first.path,
        "--workspace", second.reference, second.path,
    ]


def test_node_observation_preserves_workspace_fault_and_sibling_state() -> None:
    first = workspace("author-1")
    second = workspace("author-2")
    response = [
        {"reference": first.reference, "error": "cannot read run state"},
        {"reference": second.reference, "state": {"state": "running"}},
    ]
    with mock.patch.object(
        scheduler, "command_output_async", return_value=json.dumps(response)
    ):
        results = asyncio.run(scheduler.observe_node_states([first, second]))

    assert isinstance(results[0], scheduler.ConfigError)
    assert str(results[0]) == f"{first.reference}: cannot read run state"
    assert results[1] == {"state": "running"}


def test_invalid_node_observation_makes_every_workspace_unavailable() -> None:
    items = [workspace("author-1"), workspace("author-2")]
    with mock.patch.object(
        scheduler, "observe_node_states",
        side_effect=scheduler.ConfigError("invalid run states from NODE@node"),
    ):
        results = asyncio.run(scheduler.observe_states(items))

    assert len(results) == 2
    assert results[0] is results[1]
    assert isinstance(results[0], scheduler.ConfigError)


def test_internal_states_reports_each_workspace_independently(capsys) -> None:
    def read(path):
        if path == Path("/broken"):
            raise OSError("cannot read run state")
        return {"state": "running"} if path == Path("/active") else None

    with mock.patch.object(scheduler, "state", side_effect=read):
        assert scheduler.main([
            "_states",
            "--workspace", "broken@node", "/broken",
            "--workspace", "active@node", "/active",
        ]) == 0

    assert json.loads(capsys.readouterr().out) == [
        {"reference": "broken@node", "error": "cannot read run state"},
        {"reference": "active@node", "state": {"state": "running"}},
    ]


def test_running_workspace_capacity_is_not_reused() -> None:
    data = cluster()
    first = scheduler.workspaces(data)[0]
    with (
        mock.patch.object(
            scheduler,
            "observe_states",
            side_effect=observations(lambda item: (
                {"state": "running", "gpu": 2} if item == first else None
            )),
        ),
        mock.patch.object(
            scheduler,
            "read_queue",
            return_value=snapshot([
            ]),
        ),
        mock.patch.object(scheduler, "launch") as launch,
    ):
        assert scheduler.schedule_once(data) == 0
    launch.assert_not_called()


def test_active_slug_is_not_launched_again_before_claim_publication() -> None:
    data = cluster()
    first, _ = scheduler.workspaces(data)
    candidate = {
        "slug": "task", "capabilities": {},
        "resources": {"gpu": 0},
    }

    async def read_state(item):
        return {
            "state": "running", "slug": "task", "gpu": 0,
        } if item == first else None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_queue", return_value=snapshot([candidate])),
        mock.patch.object(scheduler, "reserve_state") as reserve,
        mock.patch.object(scheduler, "launch") as launch,
    ):
        assert scheduler.schedule_once(data) == 0

    reserve.assert_not_called()
    launch.assert_not_called()


def test_permanently_unfit_task_is_blocked() -> None:
    data = cluster()
    candidate = {"slug": "too-large", "capabilities": {}, "resources": {"gpu": 3}}
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([{**candidate}])
        ),
        mock.patch.object(scheduler, "block_unfit") as block,
    ):
        assert scheduler.schedule_once(data) == 0
    block.assert_called_once_with(scheduler.workspaces(data)[0], "too-large")


def test_unreachable_workspace_does_not_block_reachable_placement() -> None:
    data = cluster()
    candidate = {"slug": "task", "capabilities": {}, "resources": {"gpu": 0}}
    first = scheduler.workspaces(data)[0]
    selected = scheduler.workspaces(data)[1]

    def read(item):
        if item == first:
            raise scheduler.ConfigError("offline")
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([{**candidate}])
        ),
        mock.patch.object(scheduler, "reserve_state") as reserve,
        mock.patch.object(scheduler, "launch") as launch,
    ):
        assert scheduler.schedule_once(data) == 1

    reserve.assert_called_once_with(selected, "task", 0)
    launch.assert_called_once_with(selected, "task")


def test_unknown_workspace_state_hides_shared_node_capacity() -> None:
    data = cluster()
    candidate = {"slug": "task", "capabilities": {}, "resources": {"gpu": 1}}
    first = scheduler.workspaces(data)[0]

    def read(item):
        if item == first:
            raise scheduler.TransportFailure("offline")
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([{**candidate}])
        ),
        mock.patch.object(scheduler, "reserve_state") as reserve,
        mock.patch.object(scheduler, "launch") as launch,
    ):
        assert scheduler.schedule_once(data) == 0

    reserve.assert_not_called()
    launch.assert_not_called()


def test_reservation_failure_retries_another_workspace() -> None:
    data = cluster()
    candidate = {"slug": "task", "capabilities": {}, "resources": {"gpu": 0}}
    selected = scheduler.workspaces(data)[1]
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([{**candidate}])
        ),
        mock.patch.object(
            scheduler,
            "reserve_state",
            side_effect=[scheduler.ConfigError("offline"), None],
        ),
        mock.patch.object(scheduler, "launch") as launch,
    ):
        assert scheduler.schedule_once(data) == 1

    launch.assert_called_once_with(selected, "task")


def test_reservation_failure_backs_off_unchanged_workspace() -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    candidate = {
        "slug": "task", "capabilities": {},
        "resources": {"gpu": 0},
    }
    retry_after: dict[str, float] = {}
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([candidate])
        ),
        mock.patch.object(
            scheduler, "reserve_state",
            side_effect=[scheduler.TransportFailure("offline"), None, None],
        ) as reserve,
        mock.patch.object(scheduler, "launch"),
        mock.patch.object(scheduler.time, "monotonic", return_value=100.0),
    ):
        assert scheduler.schedule_once(data, retry_after) == 1
        assert retry_after[first.reference] == 130.0
        assert scheduler.schedule_once(data, retry_after) == 1

    assert reserve.call_args_list == [
        mock.call(first, "task", 0),
        mock.call(second, "task", 0),
        mock.call(second, "task", 0),
    ]


def test_checkout_fault_cordons_workspace_and_tries_another(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    data = cluster()
    first, second = scheduler.workspaces(data)
    candidate = {
        "slug": "task", "capabilities": {},
        "resources": {"gpu": 0},
    }
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(scheduler, "read_queue", return_value=snapshot([candidate])),
        mock.patch.object(
            scheduler, "reserve_state",
            side_effect=[
                scheduler.ConfigError("queue checkout is dirty:\n?? stale/repo"),
                None,
            ],
        ),
        mock.patch.object(scheduler, "launch") as launch,
    ):
        assert scheduler.schedule_once(data) == 1

    assert load_cordons("local") == {
        first.reference: "queue checkout is dirty: ?? stale/repo"
    }
    launch.assert_called_once_with(second, "task")


def test_transport_failure_backs_off_without_cordoning(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    data = cluster()
    first = scheduler.workspaces(data)[0]
    candidate = {
        "slug": "task", "capabilities": {},
        "resources": {"gpu": 0},
    }
    retry_after: dict[str, float] = {}
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(scheduler, "read_queue", return_value=snapshot([candidate])),
        mock.patch.object(
            scheduler, "reserve_state",
            side_effect=[scheduler.TransportFailure("offline"), None],
        ),
        mock.patch.object(scheduler, "launch"),
        mock.patch.object(scheduler.time, "monotonic", return_value=100.0),
    ):
        assert scheduler.schedule_once(data, retry_after) == 1

    assert load_cordons("local") == {}
    assert retry_after[first.reference] == 130.0


def test_reservation_prepares_children_before_recording_state(tmp_path: Path) -> None:
    control = mock.Mock(ref="refs/heads/main")
    with (
        mock.patch.object(scheduler.task, "coordination_lock"),
        mock.patch.object(
            scheduler.task, "resolve_default_branch", return_value=control,
        ),
        mock.patch.object(scheduler.task, "fetch_ref", return_value="remote") as fetch,
        mock.patch.object(scheduler.task, "synchronize_checkout") as synchronize,
        mock.patch.object(
            scheduler.task, "local_state", return_value=({}, {}, []),
        ),
        mock.patch.object(scheduler.task, "prepare_children") as prepare,
    ):
        assert scheduler.main([
            "_reserve", "--workspace", str(tmp_path),
            "--reference", "author@node",
            "--slug", "task", "--gpu", "1",
        ]) == 0

    fetch.assert_called_once_with(tmp_path, control.ref)
    synchronize.assert_called_once_with(tmp_path, control, "remote")
    prepare.assert_called_once_with(tmp_path)
    assert scheduler.raw_state(tmp_path)["state"] == "reserved"


def test_reservation_preserves_matching_claim_candidate(tmp_path: Path) -> None:
    control = mock.Mock(ref="refs/heads/main")
    claim = {
        "slug": "task", "owner": "author@node", "claim_id": "claim",
        "spec_sha": "a" * 40, "repositories": ["group/dependency"],
    }
    with (
        mock.patch.object(scheduler.task, "coordination_lock"),
        mock.patch.object(
            scheduler.task, "resolve_default_branch", return_value=control,
        ),
        mock.patch.object(scheduler.task, "fetch_ref", return_value="remote"),
        mock.patch.object(scheduler.task, "synchronize_checkout"),
        mock.patch.object(
            scheduler.task, "local_state",
            return_value=({"task": "Authoring"}, {}, [claim]),
        ),
        mock.patch.object(scheduler.task, "prepare_children") as prepare,
    ):
        assert scheduler.main([
            "_reserve", "--workspace", str(tmp_path),
            "--reference", "author@node",
            "--slug", "task", "--gpu", "0",
        ]) == 0

    prepare.assert_called_once_with(tmp_path, claim=claim)


def test_failed_reservation_preparation_writes_no_state(tmp_path: Path) -> None:
    with (
        mock.patch.object(scheduler.task, "coordination_lock"),
        mock.patch.object(
            scheduler.task, "resolve_default_branch",
            side_effect=scheduler.task.CoordinationError("unsafe checkout"),
        ),
    ):
        assert scheduler.main([
            "_reserve", "--workspace", str(tmp_path),
            "--reference", "author@node",
            "--slug", "task", "--gpu", "1",
        ]) == 1

    assert scheduler.raw_state(tmp_path) is None


def test_remote_reservation_failure_is_reported_as_retryable(tmp_path: Path) -> None:
    with (
        mock.patch.object(scheduler.task, "coordination_lock"),
        mock.patch.object(
            scheduler.task, "resolve_default_branch",
            side_effect=scheduler.task.RemoteAccessError("origin unavailable"),
        ),
    ):
        assert scheduler.main([
            "_reserve", "--workspace", str(tmp_path),
            "--reference", "author@node",
            "--slug", "task", "--gpu", "1",
        ]) == 75

    assert scheduler.raw_state(tmp_path) is None


def test_structural_block_names_otherwise_fitting_cordons(tmp_path: Path) -> None:
    task_root = tmp_path / "scratch/task"
    task_root.mkdir(parents=True)
    with mock.patch.object(scheduler.task, "block") as block:
        assert scheduler.main([
            "_block", "--workspace", str(tmp_path), "--slug", "task",
            "--cordoned", "author@one", "--cordoned", "author@two",
        ]) == 0

    text = (task_root / "blocker.md").read_text(encoding="utf-8")
    assert "author@one, author@two" in text
    block.assert_called_once_with(tmp_path, "task")


def test_structural_block_failure_removes_generated_blocker(tmp_path: Path) -> None:
    task_root = tmp_path / "scratch/task"
    task_root.mkdir(parents=True)
    with mock.patch.object(
        scheduler.task, "block",
        side_effect=scheduler.task.CoordinationError("claim belongs elsewhere"),
    ):
        assert scheduler.main([
            "_block", "--workspace", str(tmp_path), "--slug", "task",
        ]) == 1

    assert not (task_root / "blocker.md").exists()


def test_queue_snapshot_reads_use_coordination_lock(tmp_path: Path) -> None:
    entered = []

    @contextmanager
    def locked(path):
        entered.append(path)
        yield

    with (
        mock.patch.object(scheduler.task, "coordination_lock", side_effect=locked),
        mock.patch.object(
            scheduler, "queue_snapshot", return_value=snapshot([]),
        ),
    ):
        assert scheduler.main(["_snapshot", "--workspace", str(tmp_path)]) == 0

    assert entered == [tmp_path]


def test_snapshot_checkout_fault_has_workspace_exit_code(tmp_path: Path) -> None:
    control = mock.Mock(ref="refs/heads/main")
    with (
        mock.patch.object(scheduler.task, "coordination_lock"),
        mock.patch.object(
            scheduler.task, "resolve_default_branch", return_value=control,
        ),
        mock.patch.object(scheduler.task, "fetch_ref", return_value="remote"),
        mock.patch.object(
            scheduler.task, "synchronize_checkout",
            side_effect=scheduler.task.CoordinationError("queue checkout is dirty"),
        ),
    ):
        assert scheduler.main([
            "_snapshot", "--workspace", str(tmp_path),
        ]) == 76


def test_claimed_task_is_structurally_blocked_by_its_owner(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    data = cluster()
    owner, reader = scheduler.workspaces(data)
    set_cordon("local", owner.reference, "repair required")
    candidate = {
        "slug": "task", "workspace": owner.reference,
        "capabilities": {}, "resources": {"gpu": 1},
    }

    async def read_state(item):
        return {
            "state": "failed", "slug": "task", "claim_id": "claim",
            "ended_at": "2026-07-06T00:00:00+00:00",
        } if item == owner else None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_queue", return_value=snapshot([candidate])),
        mock.patch.object(scheduler, "block_unfit") as block,
    ):
        assert scheduler.schedule_once(data) == 0

    block.assert_called_once_with(owner, "task", [owner.reference])


def test_terminal_run_backoff_is_stable_and_does_not_block_other_workspaces() -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    claimed = {
        "slug": "claimed", "workspace": first.reference,
        "capabilities": {}, "resources": {"gpu": 0},
    }
    waiting = {
        "slug": "waiting", "capabilities": {},
        "resources": {"gpu": 0},
    }
    terminal_after: dict[str, tuple[str, float]] = {}

    async def read_state(item):
        return {
            "state": "failed", "slug": "claimed", "claim_id": "claim",
            "ended_at": "2026-07-06T00:00:00+00:00",
        } if item == first else None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([claimed, waiting]),
        ),
        mock.patch.object(scheduler, "reserve_state") as reserve,
        mock.patch.object(scheduler, "launch"),
        mock.patch.object(scheduler.time, "monotonic", return_value=100.0) as clock,
    ):
        assert scheduler.schedule_once(data, terminal_after=terminal_after) == 1
        assert terminal_after[first.reference][1] == 130.0
        clock.return_value = 110.0
        assert scheduler.schedule_once(data, terminal_after=terminal_after) == 1
        clock.return_value = 131.0
        assert scheduler.schedule_once(data, terminal_after=terminal_after) == 2

    assert mock.call(first, "claimed", 0) in reserve.call_args_list


def test_interrupted_claim_requires_operator_recovery_before_resume() -> None:
    data = cluster()
    owner, reader = scheduler.workspaces(data)
    claimed = {
        "slug": "claimed", "workspace": owner.reference,
        "capabilities": {}, "resources": {"gpu": 0},
    }

    async def read_state(item):
        return {
            "state": "interrupted", "slug": "claimed", "claim_id": "claim",
            "ended_at": "2026-07-07T12:00:00+00:00",
        } if item == owner else None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([claimed]),
        ) as read_queue,
        mock.patch.object(scheduler, "reserve_state") as reserve,
        mock.patch.object(scheduler, "launch") as launch,
        mock.patch.object(scheduler, "block_unfit") as block,
    ):
        assert scheduler.schedule_once(data) == 0

    read_queue.assert_awaited_once_with(reader)
    reserve.assert_not_called()
    launch.assert_not_called()
    block.assert_not_called()


def test_interrupted_run_retains_recorded_node_capacity() -> None:
    data = cluster()
    interrupted, reader = scheduler.workspaces(data)
    waiting = {
        "slug": "waiting", "capabilities": {},
        "resources": {"gpu": 2},
    }

    async def read_state(item):
        return {
            "state": "interrupted", "slug": "interrupted", "gpu": 1,
        } if item == interrupted else None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([waiting]),
        ) as read_queue,
        mock.patch.object(scheduler, "reserve_state") as reserve,
        mock.patch.object(scheduler, "launch") as launch,
        mock.patch.object(scheduler, "block_unfit") as block,
    ):
        assert scheduler.schedule_once(data) == 0

    read_queue.assert_awaited_once_with(reader)
    reserve.assert_not_called()
    launch.assert_not_called()
    block.assert_not_called()


def test_interrupted_claim_is_not_automatically_blocked() -> None:
    data = cluster()
    owner, _ = scheduler.workspaces(data)
    claimed = {
        "slug": "claimed", "workspace": owner.reference,
        "capabilities": {}, "resources": {"gpu": 3},
    }

    async def read_state(item):
        return {
            "state": "interrupted", "slug": "claimed", "gpu": 1,
        } if item == owner else None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([claimed]),
        ),
        mock.patch.object(scheduler, "reserve_state") as reserve,
        mock.patch.object(scheduler, "block_unfit") as block,
    ):
        assert scheduler.schedule_once(data) == 0

    reserve.assert_not_called()
    block.assert_not_called()


def test_structural_block_uses_reachable_workspace() -> None:
    data = cluster()
    candidate = {"slug": "too-large", "capabilities": {}, "resources": {"gpu": 3}}
    first = scheduler.workspaces(data)[0]
    selected = scheduler.workspaces(data)[1]

    def read(item):
        if item == first:
            raise scheduler.ConfigError("offline")
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read)),
        mock.patch.object(
            scheduler, "read_queue", return_value=snapshot([{**candidate}])
        ),
        mock.patch.object(scheduler, "block_unfit") as block,
    ):
        assert scheduler.schedule_once(data) == 0

    block.assert_called_once_with(selected, "too-large")


def test_owned_claim_is_placed_only_on_its_workspace() -> None:
    data = cluster()
    owner = scheduler.workspaces(data)[1]
    candidate = {
        "slug": "claimed",
        "capabilities": {},
        "resources": {"gpu": 0},
        "workspace": owner.reference,
    }
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler,
            "read_queue",
            return_value=snapshot([{**candidate}]),
        ),
        mock.patch.object(scheduler, "reserve_state") as reserve,
        mock.patch.object(scheduler, "launch") as launch,
    ):
        assert scheduler.schedule_once(data) == 1

    reserve.assert_called_once_with(owner, "claimed", 0)
    launch.assert_called_once_with(owner, "claimed")


def test_owned_claim_is_resumed_before_new_work() -> None:
    data = cluster()
    owner = scheduler.workspaces(data)[1]
    new = {"slug": "new", "capabilities": {}, "resources": {"gpu": 0}}
    claimed = {
        "slug": "claimed",
        "capabilities": {},
        "resources": {"gpu": 0},
        "workspace": owner.reference,
    }
    launched = []
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler,
            "read_queue",
            return_value=snapshot([
                {**claimed}, {**new},
            ]),
        ),
        mock.patch.object(scheduler, "reserve_state"),
        mock.patch.object(
            scheduler,
            "launch",
            side_effect=lambda item, slug: launched.append((item.reference, slug)),
        ),
    ):
        assert scheduler.schedule_once(data) == 2

    assert launched[0] == (owner.reference, "claimed")
    assert {slug for _, slug in launched} == {"claimed", "new"}


def test_occupied_workspace_is_removed_from_global_placement() -> None:
    data = cluster()
    workspaces = scheduler.workspaces(data)
    waiting = {"slug": "new", "capabilities": {}, "resources": {"gpu": 0}}
    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler,
            "read_queue",
            return_value=snapshot(
                [{**waiting}], [workspaces[1].reference]
            ),
        ),
        mock.patch.object(scheduler, "reserve_state") as reserve,
        mock.patch.object(scheduler, "launch"),
    ):
        assert scheduler.schedule_once(data) == 1

    reserve.assert_called_once_with(workspaces[0], "new", 0)


def test_windows_liveness_uses_process_handle_without_signaling() -> None:
    class Kernel:
        def __init__(self):
            self.opened = []
            self.closed = []

        def OpenProcess(self, access, inherit, pid):
            self.opened.append((access, inherit, pid))
            return 42

        def GetExitCodeProcess(self, handle, output):
            output._obj.value = 259
            return 1

        def CloseHandle(self, handle):
            self.closed.append(handle)

    kernel = Kernel()

    assert scheduler.windows_process_alive(123, kernel)
    assert kernel.opened == [(0x1000, False, 123)]
    assert kernel.closed == [42]


def test_stale_state_can_be_reconciled_without_writing(tmp_path: Path) -> None:
    path = scheduler.runtime_path(tmp_path, "run.json")
    scheduler.atomic_json(path, {"state": "running", "pid": 999999999})

    value = scheduler.state(tmp_path, persist=False)

    assert value is not None and value["state"] == "interrupted"
    assert scheduler.raw_state(tmp_path)["state"] == "running"


def test_reservation_without_supervisor_becomes_interrupted(tmp_path: Path) -> None:
    path = scheduler.runtime_path(tmp_path, "run.json")
    scheduler.atomic_json(path, {"state": "reserved", "slug": "task", "gpu": 1})

    value = scheduler.state(tmp_path)

    assert value is not None
    assert value["state"] == "interrupted"


def test_recover_removes_only_an_interrupted_run(tmp_path: Path) -> None:
    claim_id = "d" * 32
    run_path = scheduler.runtime_path(tmp_path, "run.json")
    log_path = scheduler.runtime_path(tmp_path, "harness.log")
    claim_path = tmp_path / "scratch/task/claim.json"
    artifact_path = tmp_path / ".workspace/evidence/result.txt"
    temporary = scheduler.runtime_path(tmp_path, "tmp") / claim_id
    scheduler.atomic_json(run_path, {
        "state": "running", "pid": 999999999, "slug": "task",
        "claim_id": claim_id,
    })
    log_path.write_text("retained log\n", encoding="utf-8")
    claim_path.parent.mkdir(parents=True)
    claim_path.write_text(json.dumps({
        "owner": "author@node", "claim_id": claim_id,
    }) + "\n")
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("retained evidence\n", encoding="utf-8")
    temporary.mkdir(parents=True)
    (temporary / "partial").write_text("discard\n", encoding="utf-8")

    assert scheduler.main([
        "_recover", "--workspace", str(tmp_path),
    ]) == 0

    assert not run_path.exists()
    assert log_path.read_text(encoding="utf-8") == "retained log\n"
    assert claim_path.is_file()
    assert artifact_path.is_file()
    assert not temporary.exists()


def test_recover_rejects_a_live_supervisor(tmp_path: Path, capsys) -> None:
    run_path = scheduler.runtime_path(tmp_path, "run.json")
    scheduler.atomic_json(run_path, {
        "state": "running", "pid": os.getpid(), "slug": "task",
    })

    assert scheduler.main([
        "_recover", "--workspace", str(tmp_path),
    ]) == 1

    assert scheduler.raw_state(tmp_path)["state"] == "running"
    assert "run is not interrupted" in capsys.readouterr().err


def test_internal_clear_is_not_available() -> None:
    with pytest.raises(scheduler.ConfigError, match="unknown internal"):
        scheduler.parse_internal(["_clear", "--workspace", "/work/author"])


def test_launch_worker_detaches_supervisor(tmp_path: Path) -> None:
    args = argparse.Namespace(
        workspace=str(tmp_path), reference="author@node", slug="task"
    )
    scheduler.atomic_json(
        scheduler.runtime_path(tmp_path, "run.json"),
        {"state": "reserved", "slug": "task", "gpu": 1},
    )
    def launch(*args, **kwargs):
        process = mock.Mock(pid=123)
        scheduler.atomic_json(
            scheduler.runtime_path(tmp_path, "run.json"),
            {"state": "running", "slug": "task", "gpu": 1, "pid": 123},
        )
        return process

    with mock.patch("subprocess.Popen", side_effect=launch) as popen:
        assert scheduler.launch_worker(args) == 0
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.args[0][:4] == [
        sys.executable, "-m", "nk", "scheduler",
    ]
    assert str(scheduler.APP_ROOT) in popen.call_args.kwargs["env"]["PYTHONPATH"]
    assert scheduler.raw_state(tmp_path) == {
        "state": "running",
        "slug": "task",
        "gpu": 1,
        "pid": 123,
    }


def test_internal_worker_dispatches_to_worker(tmp_path: Path) -> None:
    with mock.patch.object(scheduler, "worker", return_value=7) as worker:
        assert scheduler.main([
            "_worker", "--workspace", str(tmp_path),
            "--reference", "author@node", "--slug", "task",
        ]) == 7

    worker.assert_called_once_with(tmp_path, "author@node", "task")


@pytest.mark.parametrize("terminal_state", ["failed", "finishing"])
def test_launch_worker_preserves_fast_terminal_state(
    tmp_path: Path, terminal_state: str,
) -> None:
    args = argparse.Namespace(
        workspace=str(tmp_path), reference="author@node", slug="task"
    )

    def launch(*args, **kwargs):
        process = mock.Mock(pid=123)
        scheduler.atomic_json(
            scheduler.runtime_path(tmp_path, "run.json"),
            {"state": terminal_state, "slug": "task", "pid": 123},
        )
        return process

    with mock.patch("subprocess.Popen", side_effect=launch):
        assert scheduler.launch_worker(args) == 0

    assert scheduler.raw_state(tmp_path)["state"] == terminal_state


def test_worker_does_not_start_harness_before_sync_and_claim(tmp_path: Path) -> None:
    original = os.environ.pop("NK_WORKSPACE_OWNER", None)
    try:
        for _ in (None,):
            with (
                mock.patch.object(
                    scheduler.task, "claim", side_effect=scheduler.ConfigError("sync failed")
                ) as claim,
                mock.patch("subprocess.run") as harness,
            ):
                assert scheduler.worker(tmp_path, "author@node", "task") == 1

            claim.assert_called_once_with(tmp_path, "task", emit=False)
            harness.assert_not_called()
    finally:
        if original is None:
            os.environ.pop("NK_WORKSPACE_OWNER", None)
        else:
            os.environ["NK_WORKSPACE_OWNER"] = original


def test_worker_persists_claim_id_in_terminal_run_state(tmp_path: Path) -> None:
    original = os.environ.get("NK_WORKSPACE_OWNER")
    try:
        with (
            mock.patch.object(
                scheduler.task, "claim",
                return_value={"status": "claimed", "claim_id": "d" * 32},
            ),
            mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)),
        ):
            assert scheduler.worker(tmp_path, "author@node", "task") == 1

        assert scheduler.raw_state(tmp_path)["claim_id"] == "d" * 32
    finally:
        if original is None:
            os.environ.pop("NK_WORKSPACE_OWNER", None)
        else:
            os.environ["NK_WORKSPACE_OWNER"] = original


def test_worker_clears_previous_log_before_post_claim_setup(tmp_path: Path) -> None:
    log_path = scheduler.runtime_path(tmp_path, "harness.log")
    log_path.parent.mkdir(parents=True)
    log_path.write_text("previous run\n", encoding="utf-8")
    original = os.environ.get("NK_WORKSPACE_OWNER")
    try:
        with (
            mock.patch.object(
                scheduler.task, "claim",
                return_value={"status": "claimed", "claim_id": "e" * 32},
            ),
            mock.patch.object(
                scheduler.tempfile, "TemporaryDirectory",
                side_effect=RuntimeError("setup failed"),
            ),
        ):
            assert scheduler.worker(tmp_path, "author@node", "task") == 1

        assert log_path.read_text(encoding="utf-8") == "\nERROR: setup failed\n"
    finally:
        if original is None:
            os.environ.pop("NK_WORKSPACE_OWNER", None)
        else:
            os.environ["NK_WORKSPACE_OWNER"] = original


@pytest.mark.parametrize("target_queue", ["Authoring", "Done"])
def test_worker_accepts_durable_state_after_each_goal_turn(
    tmp_path: Path, target_queue: str,
) -> None:
    claim_id = "a" * 32
    temporary = scheduler.runtime_path(tmp_path, "tmp") / claim_id
    temporary.mkdir(parents=True)
    (temporary / "interrupted-marker").write_text("resume\n", encoding="utf-8")
    todo = ["# TODO", ""]
    for queue in scheduler.task.QUEUE_ORDER:
        todo.extend((f"## {queue}", ""))
        if queue == target_queue:
            todo.extend(("- [`task`](scratch/task/README.md)", ""))
    (tmp_path / "TODO.md").write_text("\n".join(todo), encoding="utf-8")

    def harness(*args, **kwargs):
        environment = kwargs["env"]
        assert {
            environment["TMPDIR"], environment["TMP"], environment["TEMP"],
            environment["NK_RUN_TEMP"],
        } == {str(temporary)}
        assert (temporary / "interrupted-marker").is_file()
        assert Path(environment["NK_RUN_PROMPT_FILE"]).read_text() == "$task-author task\n"
        Path(environment["NK_RUN_SESSION_FILE"]).parent.mkdir(parents=True, exist_ok=True)
        Path(environment["NK_RUN_SESSION_FILE"]).write_text("session-id\n")
        return mock.Mock(returncode=0)

    checkpoint_numbers = mock.Mock(
        side_effect=[[], [1]] if target_queue == "Authoring" else [[]]
    )
    with (
        mock.patch.dict(os.environ),
        mock.patch.object(
            scheduler.task, "claim",
            return_value={"status": "resumed", "claim_id": claim_id},
        ),
        mock.patch.object(scheduler.task, "checkpoint_numbers", checkpoint_numbers),
        mock.patch.object(
            scheduler.task, "task_claim",
            return_value=({}, {}, {"claim_id": claim_id}),
        ),
        mock.patch.object(scheduler.task, "ensure_published"),
        mock.patch("subprocess.run", side_effect=harness),
    ):
        assert scheduler.worker(tmp_path, "author@node", "task") == 0

    assert temporary.exists()
    assert scheduler.raw_state(tmp_path)["state"] == "finishing"
    assert scheduler.raw_state(tmp_path)["result"] == {
        "slug": "task", "queue": target_queue,
    }
    session_file = scheduler.runtime_path(tmp_path, "sessions") / f"{claim_id}.txt"
    assert session_file.exists() == (target_queue == "Authoring")
    with mock.patch.object(scheduler, "process_alive", return_value=False):
        assert scheduler.state(tmp_path)["state"] == "completed"
    assert not temporary.exists()


def test_worker_pauses_clean_authoring_turn_without_checkpoint(tmp_path: Path) -> None:
    claim_id = "a" * 32
    todo = ["# TODO", ""]
    for queue in scheduler.task.QUEUE_ORDER:
        todo.extend((f"## {queue}", ""))
        if queue == "Authoring":
            todo.extend(("- [`task`](scratch/task/README.md)", ""))
    (tmp_path / "TODO.md").write_text("\n".join(todo), encoding="utf-8")

    with (
        mock.patch.dict(os.environ),
        mock.patch.object(
            scheduler.task, "claim",
            return_value={"status": "resumed", "claim_id": claim_id},
        ),
        mock.patch.object(scheduler.task, "checkpoint_numbers", return_value=[]),
        mock.patch.object(
            scheduler.task, "task_claim",
            return_value=({}, {}, {"claim_id": claim_id}),
        ),
        mock.patch.object(scheduler.task, "ensure_published"),
        mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)),
    ):
        assert scheduler.worker(tmp_path, "author@node", "task") == 1

    assert scheduler.raw_state(tmp_path)["state"] == "interrupted"
    assert "without a Checkpoint" in scheduler.raw_state(tmp_path)["error"]


def test_worker_cleans_temporary_files_after_failure(tmp_path: Path) -> None:
    claim_id = "b" * 32

    def harness(*args, **kwargs):
        temporary = Path(kwargs["env"]["NK_RUN_TEMP"])
        (temporary / "diagnostic.txt").write_text("failed\n", encoding="utf-8")
        return mock.Mock(returncode=1)

    with (
        mock.patch.dict(os.environ),
        mock.patch.object(
            scheduler.task, "claim",
            return_value={"status": "claimed", "claim_id": claim_id},
        ),
        mock.patch.object(scheduler.task, "checkpoint_numbers", return_value=[]),
        mock.patch("subprocess.run", side_effect=harness),
    ):
        assert scheduler.worker(tmp_path, "author@node", "task") == 1

    temporary = scheduler.runtime_path(tmp_path, "tmp") / claim_id
    assert (temporary / "diagnostic.txt").read_text(encoding="utf-8") == "failed\n"
    assert scheduler.raw_state(tmp_path)["state"] == "failed"
    with mock.patch.object(scheduler, "process_alive", return_value=False):
        assert scheduler.state(tmp_path)["state"] == "failed"
    assert not temporary.exists()


def test_interrupted_worker_cleans_temporary_files(tmp_path: Path) -> None:
    claim_id = "c" * 32
    temporary = scheduler.runtime_path(tmp_path, "tmp") / claim_id
    temporary.mkdir(parents=True)
    (temporary / "partial").write_text("discard\n", encoding="utf-8")
    scheduler.atomic_json(scheduler.runtime_path(tmp_path, "run.json"), {
        "state": "running", "pid": 999999999, "claim_id": claim_id,
    })

    assert scheduler.state(tmp_path)["state"] == "interrupted"

    assert not temporary.exists()


def test_claim_temporary_directory_rejects_a_link(tmp_path: Path) -> None:
    root = scheduler.runtime_path(tmp_path, "tmp")
    root.mkdir(parents=True)
    target = root / "target"
    target.mkdir()
    linked = root / ("c" * 32)
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(scheduler.WorkspaceFault, match="temporary directory is a link"):
        scheduler.claim_temp_path(tmp_path, "c" * 32)


def test_claim_temporary_directory_rejects_a_linked_parent(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    runtime = tmp_path / ".workspace" / "nk"
    runtime.mkdir(parents=True)
    (runtime / "tmp").symlink_to(external, target_is_directory=True)

    with pytest.raises(
        scheduler.WorkspaceFault, match="temporary directory parent is a link",
    ):
        scheduler.claim_temp_path(tmp_path, "c" * 32)


def test_show_log_uses_utf8_when_stdout_defaults_to_cp1252(
    monkeypatch, tmp_path: Path
) -> None:
    log_path = scheduler.runtime_path(tmp_path, "harness.log")
    log_path.parent.mkdir(parents=True)
    log_path.write_text("passed \u2713\n", encoding="utf-8")
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(scheduler.sys, "stdout", stream)

    assert scheduler.show_log(tmp_path, False, 10) == 0
    stream.flush()
    assert raw.getvalue().decode("utf-8") == "passed \u2713\n"


def test_harness_uses_current_python_interpreter() -> None:
    assert scheduler.harness_command()[0] == sys.executable


def test_windows_supervisor_breaks_away_from_ssh_job() -> None:
    options = scheduler.detached_process_options("nt")
    flags = options["creationflags"]
    assert flags & 0x00000008
    assert flags & 0x01000000
    assert "start_new_session" not in options


def test_windows_supervisor_owns_kill_on_close_job() -> None:
    class Kernel:
        def __init__(self):
            self.limit_flags = None
            self.assigned = []
            self.closed = []

        def CreateJobObjectW(self, security, name):
            return 42

        def SetInformationJobObject(self, job, kind, value, size):
            self.limit_flags = value._obj.BasicLimitInformation.LimitFlags
            return 1

        def AssignProcessToJobObject(self, job, process):
            self.assigned.append((job, process))
            return 1

        def GetCurrentProcess(self):
            return 99

        def CloseHandle(self, handle):
            self.closed.append(handle)

    kernel = Kernel()
    active_kernel, job = scheduler.attach_windows_supervisor(kernel)

    assert active_kernel is kernel
    assert job == 42
    assert kernel.limit_flags == 0x00002000
    assert kernel.assigned == [(42, 99)]
    assert kernel.closed == []


def test_logs_defaults_to_tail_without_follow(monkeypatch) -> None:
    data = cluster()
    selected = scheduler.workspaces(data)[0]
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)
    with mock.patch("subprocess.run") as run:
        run.return_value.returncode = 0
        assert scheduler.logs("local", selected.reference, False, 50) == 0
    assert run.call_args.args[0][-5:] == [
        "scheduler", "_logs", "--workspace", selected.path, "--tail", "50"
    ][-5:]
    assert "-f" not in run.call_args.args[0]


def test_logs_follow_uses_f_flag(monkeypatch) -> None:
    data = cluster()
    selected = scheduler.workspaces(data)[0]
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)
    with mock.patch("subprocess.run") as run:
        run.return_value.returncode = 0
        scheduler.logs("local", selected.reference, True, 10)
    assert "-f" in run.call_args.args[0]


def test_recover_targets_the_owning_node(monkeypatch) -> None:
    data = cluster()
    selected = scheduler.workspaces(data)[0]
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    with mock.patch.object(scheduler, "command_output") as command:
        assert scheduler.recover("local", selected.reference) == 0

    assert command.call_args.args == (selected, [
        scheduler.node_nk(selected), "scheduler", "_recover",
        "--workspace", selected.path,
    ])


def test_claim_logs_resolves_global_id_to_workspace(monkeypatch) -> None:
    data = cluster()
    selected = scheduler.workspaces(data)[0]
    monkeypatch.setattr(scheduler, "cluster_names", lambda: ["local"])
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    async def read_state(item):
        return {
            "claim_id": "global-id", "state": "completed",
            "slug": "historical-task",
        } if item == selected else None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch("subprocess.run") as run,
    ):
        run.return_value.returncode = 0
        assert scheduler.claim_logs(None, "global-id", False, 20) == 0

    assert selected.path in run.call_args.args[0]
    assert run.call_args.args[0][-2:] == ["--tail", "20"]


def test_claim_log_resolution_rejects_duplicate_id() -> None:
    data = cluster()

    with mock.patch.object(
        scheduler, "observe_states",
        side_effect=observations({"claim_id": "duplicate"}),
    ):
        with pytest.raises(scheduler.ConfigError, match="multiple workspaces"):
            asyncio.run(scheduler.resolve_claim_workspace("duplicate", [data]))


def test_claim_log_resolution_reports_missing_id() -> None:
    data = cluster()

    with mock.patch.object(scheduler, "observe_states", side_effect=observations(None)):
        with pytest.raises(scheduler.ConfigError, match="claim ID not found"):
            asyncio.run(scheduler.resolve_claim_workspace("missing", [data]))


def test_claim_log_resolution_falls_back_to_authoritative_claim() -> None:
    data = cluster()
    selected = scheduler.workspaces(data)[0]

    async def read_state(item):
        return {"state": "running"} if item == selected else None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(
            scheduler, "read_claims",
            return_value=[{
                "owner": selected.reference,
                "slug": "task",
                "claim_id": "pre-upgrade-id",
            }],
        ),
    ):
        resolved = asyncio.run(
            scheduler.resolve_claim_workspace("pre-upgrade-id", [data])
        )

    assert resolved == selected


def test_claim_log_resolution_propagates_unexpected_state_failure() -> None:
    data = cluster()

    with mock.patch.object(
        scheduler, "observe_states",
        side_effect=observations(OSError("ssh is missing")),
    ):
        with pytest.raises(OSError, match="ssh is missing"):
            asyncio.run(scheduler.resolve_claim_workspace("claim-id", [data]))


def test_status_shows_claim_and_run_state_separately(monkeypatch, capsys) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    async def read_state(item):
        if item == first:
            return {
                "state": "running", "slug": "claimed-task",
                "claim_id": "claim-id",
            }
        return {
            "state": "completed", "slug": "previous-task",
            "claim_id": "completed-id",
        }

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(
            scheduler,
            "read_claims",
            return_value=[{
                "owner": first.reference,
                "slug": "claimed-task",
                "claim_id": "claim-id",
            }],
        ) as read_claims,
    ):
        assert scheduler.status("local") == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "WORKSPACE", "CLAIM", "ID", "RUN", "TASK", "RUN", "STATE", "SCHEDULING",
    ]
    assert lines[1].split() == [
        first.reference, "claim-id", "claimed-task", "running", "eligible",
    ]
    assert lines[2].split() == [
        second.reference, "—", "—", "idle", "eligible",
    ]
    read_claims.assert_awaited_once_with(second)


def test_status_hides_running_record_that_conflicts_with_current_claim(
    monkeypatch, capsys
) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)
    claim = {
        "owner": first.reference,
        "slug": "current-task",
        "claim_id": "current-id",
    }

    async def read_state(item):
        if item == first:
            return {
                "state": "running", "slug": "stale-task",
                "claim_id": "stale-id",
            }
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[claim]),
    ):
        assert scheduler.status("local") == 0

    assert capsys.readouterr().out.splitlines()[1].split() == [
        first.reference, "current-id", "—", "idle", "eligible",
    ]


def test_status_shows_interruption_that_conflicts_with_current_claim(
    monkeypatch, capsys
) -> None:
    data = cluster()
    first, _ = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)
    claim = {
        "owner": first.reference,
        "slug": "current-task",
        "claim_id": "current-id",
    }

    async def read_state(item):
        if item == first:
            return {
                "state": "interrupted", "slug": "stale-task",
                "claim_id": "stale-id",
            }
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[claim]),
    ):
        assert scheduler.status("local") == 0

    assert capsys.readouterr().out.splitlines()[1].split() == [
        first.reference, "current-id", "stale-task", "interrupted",
        "recovery", "required",
    ]


@pytest.mark.parametrize("run_state", ["completed", "failed"])
def test_status_hides_unclaimed_terminal_run(
    monkeypatch, capsys, run_state: str
) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    async def read_state(item):
        if item == first:
            return {
                "state": run_state, "slug": "historical-task",
                "claim_id": "historical-id",
            }
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[]),
    ):
        assert scheduler.status("local") == 0

    assert capsys.readouterr().out.splitlines()[1].split() == [
        first.reference, "—", "—", "idle", "eligible",
    ]


def test_status_shows_unclaimed_interrupted_run(monkeypatch, capsys) -> None:
    data = cluster()
    first, _ = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    async def read_state(item):
        if item == first:
            return {
                "state": "interrupted", "slug": "interrupted-task",
                "claim_id": "interrupted-id",
            }
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[]),
    ):
        assert scheduler.status("local") == 0

    assert capsys.readouterr().out.splitlines()[1].split() == [
        first.reference, "interrupted-id", "interrupted-task",
        "interrupted", "recovery", "required",
    ]


@pytest.mark.parametrize("run_state", ["failed", "interrupted"])
def test_status_keeps_terminal_run_with_matching_claim(
    monkeypatch, capsys, run_state: str
) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)
    claim = {
        "owner": first.reference,
        "slug": "current-task",
        "claim_id": "current-id",
    }

    async def read_state(item):
        if item == first:
            return {
                "state": run_state, "slug": "current-task",
                "claim_id": "current-id",
            }
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[claim]),
    ):
        assert scheduler.status("local") == 0

    scheduling = ["recovery", "required"] if run_state == "interrupted" else ["eligible"]
    assert capsys.readouterr().out.splitlines()[1].split() == [
        first.reference, "current-id", "current-task", run_state,
        *scheduling,
    ]


def test_status_reports_cordoned_workspace_and_fault(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("NK_HOME", str(tmp_path))
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)
    set_cordon("local", first.reference, "queue checkout is dirty")

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(scheduler, "read_claims", return_value=[]) as claims,
    ):
        assert scheduler.status("local") == 0

    output = capsys.readouterr().out
    assert first.reference in output
    assert "cordoned: queue checkout is dirty" in output
    claims.assert_awaited_once_with(second)


def test_wait_reports_changes_until_all_workspaces_drain() -> None:
    data = cluster()
    first, _ = scheduler.workspaces(data)
    calls: dict[str, int] = {}
    events = []

    async def read_state(item):
        calls[item.reference] = calls.get(item.reference, 0) + 1
        if item == first and calls[item.reference] == 1:
            return {"state": "running", "slug": "task"}
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(
            scheduler.output, "event",
            side_effect=lambda name, *details, **fields: events.append(
                (name, details, fields)
            ),
        ),
    ):
        assert asyncio.run(scheduler.wait_async(data, interval=0)) == 0

    assert events == [
        (
            "DRAINING", ("task", "running"),
            {"workspace": first.reference},
        ),
        ("DRAINED", ("local",), {}),
    ]


def test_wait_fails_when_drain_cannot_be_observed() -> None:
    data = cluster()
    first, _ = scheduler.workspaces(data)

    async def read_state(item):
        if item == first:
            raise scheduler.TransportFailure("offline")
        return None

    with mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)):
        with pytest.raises(
            scheduler.ConfigError, match="cannot observe cluster drain: offline"
        ):
            asyncio.run(scheduler.wait_async(data, interval=0))


def test_wait_requires_explicit_recovery_for_interrupted_run() -> None:
    data = cluster()

    with mock.patch.object(
        scheduler, "observe_states",
        side_effect=observations({"state": "interrupted", "slug": "task"}),
    ):
        with pytest.raises(
            scheduler.ConfigError,
            match="interrupted run requires recovery: author-1@node, author-2@node",
        ):
            asyncio.run(scheduler.wait_async(data, interval=0))


def test_status_matches_pre_upgrade_run_to_current_claim(monkeypatch, capsys) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)
    claim = {
        "owner": first.reference,
        "slug": "current-task",
        "claim_id": "current-id",
    }

    async def read_state(item):
        if item == first:
            return {"state": "failed", "slug": "current-task"}
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[claim]),
    ):
        assert scheduler.status("local") == 0

    assert capsys.readouterr().out.splitlines()[1].split() == [
        first.reference, "current-id", "current-task", "failed", "eligible",
    ]


@pytest.mark.parametrize("run_state", ["reserved", "running"])
def test_status_keeps_pre_claim_launch_state(
    monkeypatch, capsys, run_state: str
) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    async def read_state(item):
        if item == first:
            return {"state": run_state, "slug": "launching-task"}
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[]),
    ):
        assert scheduler.status("local") == 0

    assert capsys.readouterr().out.splitlines()[1].split() == [
        first.reference, "—", "launching-task", run_state, "eligible",
    ]


def test_status_falls_back_and_preserves_unavailable_workspace(monkeypatch, capsys) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    async def read_state(item):
        if item == first:
            raise scheduler.ConfigError("offline")
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[]),
    ):
        assert scheduler.status("local") == 1

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[1].split() == [
        first.reference, "—", "—", "unavailable", "eligible",
    ]
    assert lines[2].split() == [
        second.reference, "—", "—", "idle", "eligible",
    ]
    assert f"{first.reference}: offline" in captured.err


def test_status_marks_claims_unknown_when_every_workspace_is_busy(
    monkeypatch, capsys
) -> None:
    data = cluster()
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    with (
        mock.patch.object(
            scheduler, "observe_states",
            side_effect=observations({"state": "running", "slug": "active-task"}),
        ),
        mock.patch.object(scheduler, "read_claims") as read_claims,
    ):
        assert scheduler.status("local") == 1

    captured = capsys.readouterr()
    assert all(line.split()[1] == "—" for line in captured.out.splitlines()[1:])
    assert "no idle workspace is available to read claims" in captured.err
    read_claims.assert_not_awaited()


def test_status_claim_reader_falls_back_after_failure(monkeypatch, capsys) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(None)),
        mock.patch.object(
            scheduler, "read_claims",
            side_effect=[scheduler.ConfigError("stale"), []],
        ) as read_claims,
    ):
        assert scheduler.status("local") == 1

    assert read_claims.await_args_list == [mock.call(first), mock.call(second)]
    assert f"{first.reference}: stale" in capsys.readouterr().err


def test_status_reads_local_claims_while_remote_state_is_pending(
    monkeypatch, capsys,
) -> None:
    data = cluster()
    first = scheduler.workspaces(data)[0]
    data["nodes"].append({
        "name": "remote",
        "state": "present",
        "target": "host@example",
        "capabilities": {"os": "linux", "architecture": "x86_64"},
        "resources": {"gpu": 1},
        "workspaces": [{
            "name": "reviewer",
            "state": "present",
            "path": "/remote",
        }],
    })
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)
    remote = scheduler.workspaces(data)[-1]
    remote_started = asyncio.Event()
    release_remote = asyncio.Event()

    async def read_state(item):
        if item == remote:
            remote_started.set()
            await release_remote.wait()
        return None

    async def read_claims(item):
        assert item == first
        await remote_started.wait()
        release_remote.set()
        return []

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", side_effect=read_claims),
    ):
        assert asyncio.run(asyncio.wait_for(scheduler.status_async(data), 1)) == 0

    assert remote.reference in capsys.readouterr().out


def test_status_does_not_repeat_workspace_in_transport_error(monkeypatch, capsys) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    async def read_state(item):
        if item == first:
            raise scheduler.ConfigError(f"{first.reference}: offline")
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[]),
    ):
        assert scheduler.status("local") == 1

    assert capsys.readouterr().err.count(first.reference) == 1


def test_status_json_reports_structured_snapshot(monkeypatch, capsys) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    async def read_state(item):
        if item == first:
            return {
                "state": "running", "slug": "claimed-task",
                "claim_id": "claim-id",
            }
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(
            scheduler,
            "read_claims",
            return_value=[{
                "owner": first.reference,
                "slug": "claimed-task",
                "claim_id": "claim-id",
            }],
        ),
    ):
        assert scheduler.status("local", json_output=True) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    value = json.loads(captured.out)
    assert value == {
        "cluster": "local",
        "failed": False,
        "errors": [],
        "claims": [{
            "owner": first.reference,
            "slug": "claimed-task",
            "claim_id": "claim-id",
        }],
        "workspaces": [
            {
                "workspace": first.reference,
                "node": first.node,
                "claim_id": "claim-id",
                "claim_task": "claimed-task",
                "run_task": "claimed-task",
                "run_state": "running",
                "run_gpu": 0,
                "scheduling": "eligible",
            },
            {
                "workspace": second.reference,
                "node": second.node,
                "claim_id": None,
                "claim_task": None,
                "run_task": None,
                "run_state": "idle",
                "run_gpu": 0,
                "scheduling": "eligible",
            },
        ],
    }


def test_status_json_captures_errors_without_stderr(monkeypatch, capsys) -> None:
    data = cluster()
    first, second = scheduler.workspaces(data)
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)

    async def read_state(item):
        if item == first:
            raise scheduler.ConfigError("offline")
        return None

    with (
        mock.patch.object(scheduler, "observe_states", side_effect=observations(read_state)),
        mock.patch.object(scheduler, "read_claims", return_value=[]),
    ):
        assert scheduler.status("local", json_output=True) == 1

    captured = capsys.readouterr()
    assert captured.err == ""
    value = json.loads(captured.out)
    assert value["failed"] is True
    assert value["errors"] == [f"{first.reference}: offline"]
    assert value["workspaces"][0]["run_state"] == "unavailable"
    assert value["workspaces"][1]["workspace"] == second.reference


def test_scheduler_main_dispatches_status(monkeypatch) -> None:
    with mock.patch.object(scheduler, "status", return_value=7) as status:
        assert scheduler.main(["status", "--cluster", "local"]) == 7
    status.assert_called_once_with("local", json_output=False)


def test_scheduler_main_dispatches_status_json(monkeypatch) -> None:
    with mock.patch.object(scheduler, "status", return_value=7) as status:
        assert scheduler.main(["status", "--cluster", "local", "--json"]) == 7
    status.assert_called_once_with("local", json_output=True)


def test_scheduler_run_writes_event_log(monkeypatch) -> None:
    data = cluster()
    monkeypatch.setattr(scheduler, "get_cluster", lambda name: data)
    monkeypatch.setattr(scheduler, "verify_cluster_revision", lambda data: None)
    monkeypatch.setattr(scheduler, "application_revision", lambda: "a" * 40)
    monkeypatch.setattr(scheduler, "schedule_once", lambda *args: 0)

    try:
        assert scheduler.run("local", True, False) == 0
    finally:
        scheduler.output.log_to(None)

    path = Path(os.environ["NK_HOME"]) / "clusters/local/state/events.jsonl"
    value = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert value["event"] == "STARTED"
    assert value["message"] == "local\t2 workspaces"


def test_scheduler_main_dispatches_events(monkeypatch) -> None:
    with mock.patch.object(scheduler, "events", return_value=7) as events:
        assert scheduler.main(["events", "--cluster", "local", "--tail", "12", "-f"]) == 7
    events.assert_called_once_with("local", True, 12)


def test_internal_queue_overview_reports_json(monkeypatch, tmp_path, capsys) -> None:
    queues = {queue: [] for queue in scheduler.task.QUEUE_ORDER}
    queues["Ready"] = ["task"]
    monkeypatch.setattr(scheduler.task, "queue_overview_from_tree", lambda workspace: queues)

    assert scheduler.main(["_queues", "--workspace", str(tmp_path)]) == 0

    assert json.loads(capsys.readouterr().out) == queues


def test_scheduler_main_passes_verbose_to_run() -> None:
    with mock.patch.object(scheduler, "run", return_value=0) as run:
        assert scheduler.main([
            "run", "--cluster", "local", "--once-through", "--verbose",
        ]) == 0
    run.assert_called_once_with("local", True, True)


def test_scheduler_revision_check_accepts_aligned_nodes() -> None:
    data = cluster()
    with (
        mock.patch.object(scheduler, "application_revision", return_value="a" * 40),
        mock.patch.object(scheduler, "read_node_revision", return_value="a" * 40),
    ):
        scheduler.verify_cluster_revision(data)


def test_local_node_revision_uses_installed_node_command() -> None:
    node = cluster()["nodes"][0]
    with mock.patch.object(
        scheduler, "command_output_async", return_value="b" * 40
    ) as command:
        assert asyncio.run(scheduler.read_node_revision(node)) == "b" * 40

    command.assert_awaited_once_with(
        scheduler.node_probe(node), [scheduler.nk_command(), "scheduler", "_revision"]
    )


def test_scheduler_revision_check_rejects_mixed_nodes() -> None:
    data = cluster()
    with (
        mock.patch.object(scheduler, "application_revision", return_value="a" * 40),
        mock.patch.object(scheduler, "read_node_revision", return_value="b" * 40),
        pytest.raises(scheduler.ConfigError, match="installed nk revision mismatch"),
    ):
        scheduler.verify_cluster_revision(data)


def test_internal_revision_reports_installed_identity(capsys) -> None:
    with mock.patch.object(scheduler, "application_revision", return_value="a" * 40):
        assert scheduler.main(["_revision"]) == 0
    assert capsys.readouterr().out.strip() == "a" * 40


def test_scheduler_main_dispatches_positional_claim_logs() -> None:
    with mock.patch.object(scheduler, "claim_logs", return_value=0) as logs:
        assert scheduler.main(["logs", "global-id", "--tail", "12", "-f"]) == 0
    logs.assert_called_once_with(None, "global-id", True, 12)
