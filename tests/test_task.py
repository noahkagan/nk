from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import pytest

from nk import scheduler, task


HELPER = Path(__file__).parents[1] / "bin" / "nk"
SLUG = "2026-07-01-workflow-test"
SECOND_SLUG = "2026-07-02-second-workflow-test"


def test_relative_git_path_uses_forward_slashes_on_windows() -> None:
    workspace = PureWindowsPath("C:/workspace")
    path = workspace / "scratch" / SLUG / "claim.json"

    assert task.relative_git_path(path, workspace) == (
        f"scratch/{SLUG}/claim.json"
    )


def run(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Workflow Test",
            "GIT_AUTHOR_EMAIL": "workflow@example.invalid",
            "GIT_COMMITTER_NAME": "Workflow Test",
            "GIT_COMMITTER_EMAIL": "workflow@example.invalid",
        }
    )
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run("git", *args, cwd=repo, check=check)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_record_journal_entry_allocates_stable_newest_first_ids(tmp_path: Path) -> None:
    directory = tmp_path / "scratch" / SLUG
    write(directory / "JOURNAL.md", "# Task Journal\n")
    write(directory / "note.md", "First review.\n")

    task.record_journal_entry(
        tmp_path, SLUG, [("Review", "note.md", "QA")]
    )
    write(directory / "note.md", "Repair the candidate.\n")
    task.record_journal_entry(
        tmp_path, SLUG, [("Requested actions", "note.md", "QA")]
    )

    journal = (directory / "JOURNAL.md").read_text()
    assert journal.startswith("# Task Journal\n\n## Entry 0002\n")
    assert journal.index("Repair the candidate.") < journal.index("First review.")
    assert journal.index("## Entry 0002") < journal.index("## Entry 0001")
    assert not (directory / "note.md").exists()


@pytest.mark.parametrize(
    "journal",
    [
        (
            "# Task Journal\n\n    ````\n\n## Entry 9999\n\n"
            "    `````\n"
        ),
        (
            "# Task Journal\n\n```\nignored\n````\n\n"
            "## Entry 9999\n"
        ),
    ],
)
def test_record_journal_entry_follows_markdown_fence_structure(
    tmp_path: Path, journal: str
) -> None:
    directory = tmp_path / "scratch" / SLUG
    write(directory / "JOURNAL.md", journal)
    write(directory / "note.md", "Review passed.\n")

    task.record_journal_entry(
        tmp_path, SLUG, [("Review", "note.md", "QA")]
    )

    assert (directory / "JOURNAL.md").read_text().startswith(
        "# Task Journal\n\n## Entry 10000\n"
    )


@pytest.mark.parametrize(
    ("journal", "note", "message"),
    [
        (None, "Review passed.\n", "Journal is missing"),
        ("# Task Journal\n", "# Review\n\nPassed.\n", "without H1 or H2"),
        ("# Task Journal\n", "  ## Review\n\nPassed.\n", "without H1 or H2"),
        ("# Task Journal\n", "Review\n======\n", "without H1 or H2"),
        ("# Task Journal\n", "Review\n------\n", "without H1 or H2"),
        (
            "# Task Journal\n\n ## Entry 9999\n\nIndented entry.\n",
            "Review passed.\n",
            "non-entry H1 or H2",
        ),
        (
            "# Task Journal\n\n## Entry 0002\n\nNewer.\n\n"
            "## Entry 0003\n\nOlder.\n",
            "Review passed.\n",
            "newest first",
        ),
    ],
)
def test_record_journal_entry_rejects_invalid_input_without_mutation(
    tmp_path: Path, journal: str | None, note: str, message: str
) -> None:
    directory = tmp_path / "scratch" / SLUG
    if journal is not None:
        write(directory / "JOURNAL.md", journal)
    write(directory / "note.md", note)

    with pytest.raises(task.CoordinationError, match=message):
        task.record_journal_entry(
            tmp_path, SLUG, [("Review", "note.md", "QA")]
        )

    if journal is not None:
        assert (directory / "JOURNAL.md").read_text() == journal
    assert (directory / "note.md").read_text() == note


def test_check_rejects_legacy_journal_headings(tmp_path: Path) -> None:
    write(tmp_path / "TODO.md", todo("Ready"))
    write(tmp_path / "scratch" / SLUG / "README.md", "# Test task\n")
    write(
        tmp_path / "scratch" / SLUG / "task.json",
        json.dumps({"dependencies": [], "capabilities": {}, "resources": {}}),
    )
    write(
        tmp_path / "scratch" / SLUG / "JOURNAL.md",
        "# Named Journal\n\n## Entry 0001 — Titled entry\n",
    )

    with pytest.raises(task.CoordinationError, match="must begin with '# Task Journal'"):
        task.check(tmp_path, SLUG)


def test_checkout_dependencies_use_file_io(monkeypatch, tmp_path: Path) -> None:
    first = "2026-07-01-first"
    second = "2026-07-02-second"
    for slug, dependencies in ((first, [second]), (second, [])):
        write(
            tmp_path / "scratch" / slug / "task.json",
            json.dumps(
                {"dependencies": dependencies, "capabilities": {}, "resources": {}}
            ),
        )

    def reject_git(*args, **kwargs):
        raise AssertionError("dependency discovery invoked Git")

    monkeypatch.setattr(task, "git", reject_git)

    assert task.dependencies_from_checkout(
        tmp_path, {first: "Ready", second: "Done"}
    ) == {first: [second], second: []}


def test_exact_claim_reads_only_requested_manifest(monkeypatch, tmp_path: Path) -> None:
    unrelated = "2026-07-03-unrelated-backlog"
    observed = []

    def manifest(_workspace, _tree, path):
        observed.append(path)
        assert path == f"scratch/{SLUG}/task.json"
        return {"dependencies": [], "capabilities": {}, "resources": {}}

    monkeypatch.setattr(
        task, "text_from_tree",
        lambda *_args: todo_entries([(SLUG, "Ready"), (unrelated, "Backlog")]),
    )
    monkeypatch.setattr(task, "claims_from_tree", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(task, "owner", lambda _workspace: "author@node")
    monkeypatch.setattr(task, "optional_json_from_tree", manifest)

    assert task.select_task(tmp_path, "tree", SLUG) == (SLUG, None)
    assert observed == [f"scratch/{SLUG}/task.json"]


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def remote_sha(repo: Path, ref: str) -> str | None:
    result = git(repo, "ls-remote", "--exit-code", "origin", ref, check=False)
    if result.returncode == 2:
        return None
    assert result.returncode == 0, result.stderr
    return result.stdout.split()[0]


def reject_remote_ref(bare: Path, ref: str) -> None:
    hook = bare / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new updated_ref\n"
        "do\n"
        f'  if [ "$updated_ref" = "{ref}" ]; then\n'
        "    echo rejected by workflow test >&2\n"
        "    exit 1\n"
        "  fi\n"
        "done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def move_remote_ref_after_push_once(bare: Path, ref: str, target: str) -> None:
    hook = bare / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new updated_ref\n"
        "do\n"
        f'  if [ "$updated_ref" = "{ref}" ] && [ ! -e "$GIT_DIR/moved-once" ]; then\n'
        '    touch "$GIT_DIR/moved-once"\n'
        f'    git update-ref "$updated_ref" "{target}" "$new"\n'
        "  fi\n"
        "done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def advance_remote_after_push(bare: Path, ref: str) -> None:
    hook = bare / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new updated_ref\n"
        "do\n"
        f'  if [ "$updated_ref" = "{ref}" ] && [ ! -e "$GIT_DIR/advanced-once" ]; then\n'
        '    touch "$GIT_DIR/advanced-once"\n'
        '    tree=$(git rev-parse "$new^{tree}")\n'
        '    advanced=$(printf "concurrent control change\\n" | git commit-tree "$tree" -p "$new")\n'
        '    git update-ref "$updated_ref" "$advanced" "$new"\n'
        "  fi\n"
        "done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def advance_control_after_child_push(
    child: Path, control: Path, control_ref: str
) -> None:
    hook = child / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new updated_ref\n"
        "do\n"
        '  if [ "$updated_ref" = "refs/heads/main" ] && [ ! -e "$GIT_DIR/advanced-control" ]; then\n'
        '    touch "$GIT_DIR/advanced-control"\n'
        f'    current=$(git --git-dir="{control}" rev-parse "{control_ref}")\n'
        f'    tree=$(git --git-dir="{control}" rev-parse "$current^{{tree}}")\n'
        f'    advanced=$(printf "concurrent queue change\\n" | git --git-dir="{control}" commit-tree "$tree" -p "$current")\n'
        f'    git --git-dir="{control}" update-ref "{control_ref}" "$advanced" "$current"\n'
        "  fi\n"
        "done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def move_candidate_after_other_target_push(
    child: Path,
    candidate_repo: Path,
    candidate_ref: str,
    candidate_sha: str,
) -> None:
    hook = child / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new updated_ref\n"
        "do\n"
        '  if [ "$updated_ref" = "refs/heads/main" ] && [ ! -e "$GIT_DIR/moved-candidate" ]; then\n'
        '    touch "$GIT_DIR/moved-candidate"\n'
        f'    current=$(git --git-dir="{candidate_repo}" rev-parse "{candidate_ref}")\n'
        f'    git --git-dir="{candidate_repo}" update-ref "{candidate_ref}" "{candidate_sha}" "$current"\n'
        "  fi\n"
        "done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def bare_repo(
    tmp_path: Path, name: str, files: dict[str, str], default_branch: str = "main"
) -> Path:
    bare = tmp_path / f"{name}.git"
    run("git", "init", "--bare", f"--initial-branch={default_branch}", str(bare))
    seed = tmp_path / f"{name}-seed"
    run("git", "clone", str(bare), str(seed))
    for relative, text in files.items():
        write(seed / relative, text)
    commit(seed, "initial")
    git(seed, "push", "-u", "origin", default_branch)
    run(
        "git",
        "--git-dir",
        str(bare),
        "symbolic-ref",
        "HEAD",
        f"refs/heads/{default_branch}",
    )
    return bare


def todo(bucket: str) -> str:
    buckets = list(task.QUEUE_ORDER)
    lines: list[str] = []
    for name in buckets:
        lines.extend([f"## {name}", ""])
        if name == bucket:
            lines.extend([f"- [`{SLUG}`](scratch/{SLUG}/README.md)", ""])
    return "\n".join(lines)


def todo_entries(entries: list[tuple[str, str]]) -> str:
    buckets = list(task.QUEUE_ORDER)
    lines: list[str] = []
    for name in buckets:
        lines.extend([f"## {name}", ""])
        for slug, bucket in entries:
            if name == bucket:
                lines.extend([f"- [`{slug}`](scratch/{slug}/README.md)", ""])
    return "\n".join(lines)


def test_status_reports_task_bucket(tmp_path: Path) -> None:
    write(tmp_path / "TODO.md", todo("Ready"))

    result = run(
        sys.executable,
        str(HELPER),
        "task",
        "status",
        "--workspace",
        str(tmp_path),
        "--slug",
        SLUG,
    )

    assert result.stdout == f"STATUS\t{SLUG}\tReady\n"


def test_coordination_lock_blocks_competing_mutation(tmp_path: Path) -> None:
    code = "\n".join([
        "import sys",
        "from pathlib import Path",
        "from nk import task",
        "print('READY', flush=True)",
        "with task.coordination_lock(Path(sys.argv[1])):",
        "    print('ACQUIRED')",
    ])
    with task.coordination_lock(tmp_path):
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stdout.readline() == "READY\n"
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.1)

    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0
    assert stdout == "ACQUIRED\n"
    assert stderr == ""


def test_windows_blocking_lock_retries_contention(monkeypatch) -> None:
    calls = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_NBLCK = 2
        LK_UNLCK = 3

        @staticmethod
        def locking(file_descriptor, mode, size):
            calls.append((file_descriptor, mode, size))
            if len(calls) == 1:
                raise OSError(task.errno.EACCES, "busy")

    class Handle:
        @staticmethod
        def fileno():
            return 7

    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(task.os, "name", "nt")

    task._acquire_file_lock(Handle())

    assert calls == [(7, FakeMsvcrt.LK_LOCK, 1)] * 2


def test_status_rejects_missing_task(tmp_path: Path) -> None:
    write(tmp_path / "TODO.md", todo_entries([]))

    result = run(
        sys.executable,
        str(HELPER),
        "task",
        "status",
        "--workspace",
        str(tmp_path),
        "--slug",
        SLUG,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr == f"ERROR\ttask is missing from TODO: {SLUG}\n"


def test_claim_snapshot_reads_remote_tree_without_changing_checkout(world: World) -> None:
    claim(world, world.author)
    before = git(world.reviewer, "rev-parse", "HEAD").stdout.strip()
    lock = world.reviewer / ".workspace" / "task-coordination.lock"
    write(world.reviewer / "local.txt", "keep\n")

    claims = task.claim_snapshot(world.reviewer)

    assert claims == [{
        "owner": f"{world.author.name}@localhost",
        "claim_id": claims[0]["claim_id"],
        "spec_sha": claims[0]["spec_sha"],
        "repositories": [],
        "slug": SLUG,
    }]
    assert git(world.reviewer, "rev-parse", "HEAD").stdout.strip() == before
    assert SLUG in todo_section(world.reviewer, "Ready")
    assert (world.reviewer / "local.txt").read_text(encoding="utf-8") == "keep\n"
    assert lock.is_file()


def test_claim_snapshot_reads_cached_remote_commit_without_fetch_or_lock(
    world: World, monkeypatch,
) -> None:
    claim(world, world.author)
    expected = task.claim_snapshot(world.reviewer)

    def reject(*args, **kwargs):
        raise AssertionError("cached claim snapshot fetched or locked the checkout")

    monkeypatch.setattr(task, "fetch_ref", reject)
    monkeypatch.setattr(task, "coordination_lock", reject)

    assert task.claim_snapshot(world.reviewer) == expected


def test_claim_snapshot_reads_advertised_commit_when_branch_advances_before_fetch(
    world: World, monkeypatch,
) -> None:
    claim(world, world.author)
    original_fetch = task.fetch_ref

    def advance_then_fetch(repo: Path, ref: str) -> str:
        write(world.author / "TODO.md", todo("Ready"))
        (world.author / "scratch" / SLUG / "claim.json").unlink()
        commit(world.author, "advance after claim observation")
        git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
        return original_fetch(repo, ref)

    monkeypatch.setattr(task, "fetch_ref", advance_then_fetch)

    claims = task.claim_snapshot(world.reviewer)

    assert claims == [{
        "owner": f"{world.author.name}@localhost",
        "claim_id": claims[0]["claim_id"],
        "spec_sha": claims[0]["spec_sha"],
        "repositories": [],
        "slug": SLUG,
    }]


def test_queue_overview_reads_remote_tree_without_changing_checkout(world: World) -> None:
    before = git(world.reviewer, "rev-parse", "HEAD").stdout.strip()
    write(world.reviewer / "local.txt", "keep\n")

    overview = task.queue_overview_from_tree(world.reviewer)

    assert overview["Ready"] == [SLUG]
    assert all(overview[queue] == [] for queue in task.QUEUE_ORDER if queue != "Ready")
    assert git(world.reviewer, "rev-parse", "HEAD").stdout.strip() == before
    assert (world.reviewer / "local.txt").read_text(encoding="utf-8") == "keep\n"


@dataclass
class World:
    author: Path
    reviewer: Path
    child_bares: dict[str, Path]
    control_branch: str

    def coordinate(self, workspace: Path, command: str, *args: str, check: bool = True):
        return run(
            sys.executable,
            str(HELPER),
            "task",
            command,
            "--workspace",
            str(workspace),
            *args,
            check=check,
        )

    def sync(self, workspace: Path) -> None:
        git(workspace, "pull", "--ff-only", "origin", self.control_branch)

    def make_candidate(self, workspace: Path, repository: str, text: str) -> str:
        repo = workspace / repository
        git(repo, "switch", "-C", f"candidate/{SLUG}", "origin/main")
        write(repo / "value.txt", text)
        sha = commit(repo, f"candidate {repository}")
        git(repo, "push", "-u", "origin", f"HEAD:refs/heads/candidate/{SLUG}")
        return sha


@pytest.fixture
def world(tmp_path: Path) -> World:
    control_branch = "queue"
    control = bare_repo(
        tmp_path,
        "control",
        {
            ".gitignore": ".workspace/\ngroup/\n",
            "TODO.md": todo("Ready"),
            f"scratch/{SLUG}/README.md": "# Test task\n",
            f"scratch/{SLUG}/JOURNAL.md": "# Task Journal\n",
            f"scratch/{SLUG}/task.json": json.dumps(
                {"dependencies": [], "capabilities": {}, "resources": {}}
            ) + "\n",
        },
        default_branch=control_branch,
    )
    author = tmp_path / "author"
    reviewer = tmp_path / "reviewer"
    run("git", "clone", str(control), str(author))
    run("git", "clone", str(control), str(reviewer))

    child_bares: dict[str, Path] = {}
    for name in ("dependency", "consumer"):
        relative = f"group/{name}"
        child_bares[relative] = bare_repo(tmp_path, name, {"value.txt": "base\n"})
        for workspace in (author, reviewer):
            destination = workspace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            run("git", "clone", str(child_bares[relative]), str(destination))
    return World(author, reviewer, child_bares, control_branch)


def claim(world: World, workspace: Path) -> str:
    result = world.coordinate(workspace, "claim")
    assert result.stdout.startswith(("CLAIMED\t", "RESUMED\t"))
    return result.stdout.split("\t")[2]


def claim_with_repositories(world: World, *repositories: str) -> str:
    manifest_path = world.author / "scratch" / SLUG / "task.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repositories"] = list(repositories)
    write(manifest_path, json.dumps(manifest))
    commit(world.author, "declare task repositories")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    return claim(world, world.author)


def test_checkpoint_publishes_progress_without_releasing_claim(world: World) -> None:
    claim_id = claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Established the failing boundary.\n\nNext: repair it.\n")

    result = world.coordinate(world.author, "checkpoint", SLUG)

    assert result.stdout == f"CHECKPOINTED\t{SLUG}\tAuthoring\n"
    assert SLUG in todo_section(world.author, "Authoring")
    assert json.loads(
        (world.author / "scratch" / SLUG / "claim.json").read_text()
    )["claim_id"] == claim_id.strip()
    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    assert "**Checkpoint**" in journal
    assert "Established the failing boundary." in journal
    assert not progress.exists()
    assert git(world.author, "status", "--short").stdout == ""
    assert git(world.author, "rev-parse", "HEAD").stdout == git(
        world.author, "rev-parse", f"origin/{world.control_branch}"
    ).stdout


def test_checkpoint_can_defer_scheduler_resume(world: World) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "External operation 123 is pending; resume from its result.\n")

    result = world.coordinate(
        world.author, "checkpoint", SLUG,
        "--resume-after", "2030-01-02T03:04:05-08:00",
    )

    assert result.stdout == f"CHECKPOINTED\t{SLUG}\tAuthoring\n"
    claim_data = json.loads(
        (world.author / "scratch" / SLUG / "claim.json").read_text()
    )
    assert claim_data["resume_after"] == "2030-01-02T11:04:05Z"


def test_checkpoint_rejects_resume_after_without_timezone(world: World) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Preserve this pending result.\n")

    result = world.coordinate(
        world.author, "checkpoint", SLUG,
        "--resume-after", "2030-01-02T03:04:05", check=False,
    )

    assert result.returncode == 1
    assert "ISO 8601 timestamp with timezone" in result.stderr
    assert progress.exists()


def test_checkpoint_publishes_committed_task_companions(world: World) -> None:
    claim(world, world.author)
    companion = world.author / "scratch" / SLUG / "evidence" / "result.txt"
    write(companion, "retained evidence\n")
    commit(world.author, "retain task evidence")
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Retained the exact result.\n")

    task.checkpoint(world.author, SLUG)

    assert companion.read_text() == "retained evidence\n"
    assert git(world.author, "rev-parse", "HEAD").stdout == git(
        world.author, "rev-parse", f"origin/{world.control_branch}"
    ).stdout


def test_checkpoint_merges_companions_with_concurrent_queue_update(world: World) -> None:
    claim(world, world.author)
    companion = world.author / "scratch" / SLUG / "evidence" / "result.txt"
    write(companion, "retained evidence\n")
    commit(world.author, "retain task evidence")
    write(world.author / "scratch" / SLUG / "progress.md", "Retained evidence.\n")
    git(world.reviewer, "pull", "--ff-only")
    write(world.reviewer / "concurrent.txt", "independent queue update\n")
    remote = commit(world.reviewer, "advance queue concurrently")
    git(world.reviewer, "push", "origin", f"HEAD:{world.control_branch}")

    task.checkpoint(world.author, SLUG)

    assert git(world.author, "merge-base", "--is-ancestor", remote, "HEAD").returncode == 0
    assert (world.author / "concurrent.txt").read_text() == "independent queue update\n"
    assert companion.read_text() == "retained evidence\n"


def test_checkpoint_rejects_every_committed_protected_path(world: World) -> None:
    claim(world, world.author)
    directory = world.author / "scratch" / SLUG
    protected = {
        world.author / "TODO.md": "TODO.md",
        directory / "README.md": f"scratch/{SLUG}/README.md",
        directory / "task.json": f"scratch/{SLUG}/task.json",
        directory / "JOURNAL.md": f"scratch/{SLUG}/JOURNAL.md",
        directory / "claim.json": f"scratch/{SLUG}/claim.json",
        directory / "candidate.json": f"scratch/{SLUG}/candidate.json",
        directory / "validation.json": f"scratch/{SLUG}/validation.json",
        directory / "merge.json": f"scratch/{SLUG}/merge.json",
        directory / "progress.md": f"scratch/{SLUG}/progress.md",
        directory / "blocker.md": f"scratch/{SLUG}/blocker.md",
        directory / "cancellation.md": f"scratch/{SLUG}/cancellation.md",
        directory / "resolution.md": f"scratch/{SLUG}/resolution.md",
        directory / "follow-up.md": f"scratch/{SLUG}/follow-up.md",
    }
    for path in protected:
        write(path, path.read_text() + "\nprotected edit\n" if path.exists() else "protected\n")
    write(world.author / "outside.txt", "unrelated\n")
    git(
        world.author, "add", "-f", "--",
        *(str(path.relative_to(world.author)) for path in protected),
    )
    commit(world.author, "attempt protected task edits")

    with pytest.raises(task.CoordinationError) as error:
        task.checkpoint(world.author, SLUG)

    message = str(error.value)
    for relative in [*protected.values(), "outside.txt"]:
        assert relative in message
    for pattern, _, _ in task.CHECKPOINT_PROTECTED_PATTERNS:
        assert pattern in message
    assert "nk task checkpoint" in message
    assert (directory / "progress.md").exists()


def test_checkpoint_rejects_uncommitted_task_companion(world: World) -> None:
    claim(world, world.author)
    directory = world.author / "scratch" / SLUG
    write(directory / "progress.md", "Preserve this progress.\n")
    write(directory / "evidence" / "result.txt", "uncommitted evidence\n")

    with pytest.raises(task.CoordinationError, match="queue checkout is dirty"):
        task.checkpoint(world.author, SLUG)

    assert (directory / "progress.md").exists()
    assert (directory / "evidence" / "result.txt").exists()


def test_checkpoint_rejects_protected_path_reverted_by_later_commit(world: World) -> None:
    claim(world, world.author)
    todo = world.author / "TODO.md"
    original = todo.read_text()
    write(todo, original + "\nprotected edit\n")
    commit(world.author, "edit protected queue state")
    write(todo, original)
    commit(world.author, "revert protected queue state")
    directory = world.author / "scratch" / SLUG
    write(directory / "evidence" / "result.txt", "allowed companion\n")
    commit(world.author, "retain allowed companion")
    write(directory / "progress.md", "Preserve the complete history.\n")

    with pytest.raises(task.CoordinationError) as error:
        task.checkpoint(world.author, SLUG)

    assert "TODO.md: protected queue placement" in str(error.value)
    assert (directory / "progress.md").exists()


def test_checkpoint_rejects_local_merge_history(world: World) -> None:
    claim(world, world.author)
    directory = world.author / "scratch" / SLUG
    git(world.author, "switch", "-c", "companion-side")
    write(directory / "evidence" / "side.txt", "side companion\n")
    commit(world.author, "retain side companion")
    git(world.author, "switch", world.control_branch)
    write(directory / "evidence" / "main.txt", "main companion\n")
    commit(world.author, "retain main companion")
    git(world.author, "merge", "--no-edit", "companion-side")
    write(directory / "progress.md", "Preserve the merge for repair.\n")

    with pytest.raises(task.CoordinationError, match="contains merge commits"):
        task.checkpoint(world.author, SLUG)

    assert (directory / "progress.md").exists()


def test_checkpoint_rejects_renamed_protected_source(world: World) -> None:
    claim(world, world.author)
    directory = world.author / "scratch" / SLUG
    destination = directory / "evidence" / "claim-record.json"
    destination.parent.mkdir(parents=True)
    git(world.author, "mv", str(directory / "claim.json"), str(destination))
    commit(world.author, "rename protected claim as companion")
    write(directory / "progress.md", "Preserve the rename for repair.\n")

    with pytest.raises(task.CoordinationError) as error:
        task.checkpoint(world.author, SLUG)

    assert f"scratch/{SLUG}/claim.json: protected task claim" in str(error.value)
    assert destination.exists()
    assert (directory / "progress.md").exists()


def test_checkpoint_rejects_remote_relocation_of_edited_companion(
    world: World,
) -> None:
    directory = world.author / "scratch" / SLUG
    companion = directory / "evidence" / "result.txt"
    write(companion, "base evidence\n")
    commit(world.author, "add shared companion")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    claim(world, world.author)
    git(world.reviewer, "pull", "--ff-only")
    write(companion, "locally refined evidence\n")
    local = commit(world.author, "refine allowed companion")
    destination = world.reviewer / "scratch" / "other-task" / "result.txt"
    destination.parent.mkdir(parents=True)
    git(
        world.reviewer, "mv", f"scratch/{SLUG}/evidence/result.txt",
        "scratch/other-task/result.txt",
    )
    commit(world.reviewer, "relocate companion outside claimed task")
    git(world.reviewer, "push", "origin", f"HEAD:{world.control_branch}")
    progress = directory / "progress.md"
    write(progress, "Preserve the rejected relocation.\n")

    with pytest.raises(task.CoordinationError) as error:
        task.checkpoint(world.author, SLUG)

    assert "scratch/other-task/result.txt: outside the claimed task directory" in str(error.value)
    assert git(world.author, "rev-parse", "HEAD").stdout.strip() == local
    assert companion.read_text() == "locally refined evidence\n"
    assert progress.read_text() == "Preserve the rejected relocation.\n"


def test_checkpoint_companion_conflict_preserves_local_work(world: World) -> None:
    claim(world, world.author)
    directory = world.author / "scratch" / SLUG
    companion = directory / "evidence" / "result.txt"
    write(companion, "local evidence\n")
    local = commit(world.author, "retain local evidence")
    progress = directory / "progress.md"
    write(progress, "Preserve the conflict for repair.\n")
    git(world.reviewer, "pull", "--ff-only")
    remote_companion = world.reviewer / "scratch" / SLUG / "evidence" / "result.txt"
    write(remote_companion, "remote evidence\n")
    commit(world.reviewer, "retain conflicting evidence")
    git(world.reviewer, "push", "origin", f"HEAD:{world.control_branch}")

    with pytest.raises(task.CoordinationError, match="companion commits conflict"):
        task.checkpoint(world.author, SLUG)

    assert git(world.author, "rev-parse", "HEAD").stdout.strip() == local
    assert companion.read_text() == "local evidence\n"
    assert progress.read_text() == "Preserve the conflict for repair.\n"


def test_checkpoint_rejects_all_dirty_claim_repositories_before_mutation(
    world: World,
) -> None:
    claim_with_repositories(world, "group/dependency", "group/consumer")
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Preserve this progress.\n")
    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    control = git(world.author, "rev-parse", "HEAD").stdout
    exclude = Path(
        git(world.author, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    )
    if not exclude.is_absolute():
        exclude = world.author / exclude
    exclude_before = exclude.read_bytes()
    claim_path = world.author / "scratch" / SLUG / "claim.json"
    claim_before = claim_path.read_bytes()
    git(world.reviewer, "pull", "--ff-only")
    write(world.reviewer / "concurrent.txt", "remote advance\n")
    commit(world.reviewer, "advance remote before dirty rejection")
    git(world.reviewer, "push", "origin", f"HEAD:{world.control_branch}")
    write(world.author / "group/dependency/value.txt", "tracked edit\n")
    write(world.author / "group/consumer/untracked.txt", "untracked edit\n")

    with pytest.raises(task.CoordinationError) as error:
        task.checkpoint(world.author, SLUG)

    message = str(error.value)
    assert message.index("group/consumer:") < message.index("group/dependency:")
    assert " M value.txt" in message
    assert "?? untracked.txt" in message
    assert progress.read_text() == "Preserve this progress.\n"
    assert (world.author / "scratch" / SLUG / "JOURNAL.md").read_text() == journal
    assert git(world.author, "rev-parse", "HEAD").stdout == control
    assert git(world.author, "rev-parse", f"origin/{world.control_branch}").stdout == control
    assert exclude.read_bytes() == exclude_before
    assert claim_path.read_bytes() == claim_before

    commit(world.author / "group/dependency", "commit tracked repair")
    commit(world.author / "group/consumer", "commit untracked repair")
    task.checkpoint(world.author, SLUG)
    assert not progress.exists()


def test_checkpoint_ignores_unclaimed_and_git_ignored_files(world: World) -> None:
    claim_with_repositories(world, "group/dependency")
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Only claimed repositories matter.\n")
    exclude = world.author / "group/dependency/.git/info/exclude"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("ignored.txt\n")
    write(world.author / "group/dependency/ignored.txt", "ignored\n")
    write(world.author / "group/consumer/unclaimed.txt", "outside claim\n")

    task.checkpoint(world.author, SLUG)

    assert not progress.exists()
    assert (world.author / "group/consumer/unclaimed.txt").exists()


def local_unpushed_candidate(world: World) -> tuple[Path, str]:
    claim_with_repositories(world, "group/dependency")
    repo = world.author / "group/dependency"
    git(repo, "switch", "-c", f"candidate/{SLUG}", "origin/main")
    write(repo / "repair.txt", "local repair\n")
    return repo, commit(repo, "local repair")


def test_resumed_claim_retains_clean_unpushed_candidate(world: World) -> None:
    repo, local = local_unpushed_candidate(world)
    claim_id = json.loads(
        (world.author / "scratch" / SLUG / "claim.json").read_text()
    )["claim_id"]

    outcome = task.claim(world.author, SLUG, emit=False)

    assert outcome == {"status": "resumed", "slug": SLUG, "claim_id": claim_id}
    assert git(repo, "branch", "--show-current").stdout.strip() == f"candidate/{SLUG}"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == local


@pytest.mark.parametrize("route", ["block", "cancel"])
def test_claim_release_requires_pushed_candidate(
    world: World, route: str,
) -> None:
    repo, local = local_unpushed_candidate(world)
    directory = world.author / "scratch" / SLUG
    claim_before = (directory / "claim.json").read_bytes()
    input_path = directory / ("blocker.md" if route == "block" else "cancellation.md")
    write(input_path, f"{route.title()} for an external reason.\n")

    result = world.coordinate(world.author, route, SLUG, check=False)

    assert result.returncode == 1
    assert "not fully pushed" in result.stderr
    assert input_path.exists()
    assert (directory / "claim.json").read_bytes() == claim_before
    assert SLUG in todo_section(world.author, "Authoring")

    git(repo, "push", "-u", "origin", f"HEAD:candidate/{SLUG}")
    assert remote_sha(repo, f"refs/heads/candidate/{SLUG}") == local
    retry = world.coordinate(world.author, route, SLUG)
    assert retry.stdout.startswith("BLOCKED\t" if route == "block" else "CANCELLED\t")


def test_claim_release_rejects_dirty_candidate_before_consuming_input(
    world: World,
) -> None:
    repo, _ = local_unpushed_candidate(world)
    git(repo, "push", "-u", "origin", f"HEAD:candidate/{SLUG}")
    write(repo / "repair.txt", "uncommitted repair\n")
    directory = world.author / "scratch" / SLUG
    blocker = directory / "blocker.md"
    write(blocker, "External reason.\n")
    claim_before = (directory / "claim.json").read_bytes()

    result = world.coordinate(world.author, "block", SLUG, check=False)

    assert result.returncode == 1
    assert "claimed candidate repositories are dirty" in result.stderr
    assert blocker.read_text() == "External reason.\n"
    assert (directory / "claim.json").read_bytes() == claim_before
    assert SLUG in todo_section(world.author, "Authoring")


def test_checkpoint_push_failure_preserves_clean_recoverable_input(
    world: World, monkeypatch,
) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Keep this evidence.\n")
    before = git(world.author, "rev-parse", "HEAD").stdout
    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    monkeypatch.setattr(
        task, "push_control_ref",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "rejected"),
    )

    with pytest.raises(task.PublicationError, match="push failed"):
        task.checkpoint(world.author, SLUG)

    assert progress.read_text() == "Keep this evidence.\n"
    assert (world.author / "scratch" / SLUG / "JOURNAL.md").read_text() == journal
    assert git(world.author, "rev-parse", "HEAD").stdout == before
    assert git(world.author, "status", "--short").stdout == ""


def test_resumed_claim_preserves_unpublished_progress(world: World) -> None:
    claim_id = claim(world, world.author).strip()
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Resume from here.\n")

    outcome = task.claim(world.author, SLUG, emit=False)

    assert outcome == {"status": "resumed", "slug": SLUG, "claim_id": claim_id}
    assert progress.read_text() == "Resume from here.\n"
    assert git(world.author, "status", "--short").stdout == ""


def test_checkpoint_retry_reconciles_ambiguous_success(
    world: World, monkeypatch,
) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Publish this once.\n")
    publish = task.commit_and_push

    def ambiguous(*args, **kwargs):
        publish(*args, **kwargs)
        raise task.PublicationError("connection lost after publication")

    monkeypatch.setattr(task, "commit_and_push", ambiguous)
    with pytest.raises(task.PublicationError, match="connection lost"):
        task.checkpoint(world.author, SLUG)

    monkeypatch.setattr(task, "commit_and_push", publish)
    task.checkpoint(world.author, SLUG)

    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    assert journal.count("**Checkpoint**") == 1
    assert journal.count("Publish this once.") == 1
    assert not progress.exists()
    assert git(world.author, "status", "--short").stdout == ""


def test_checkpoint_retry_reconciles_unclosed_fence(
    world: World, monkeypatch,
) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Captured output:\n\n```text\nstill open\n")
    publish = task.commit_and_push

    def ambiguous(*args, **kwargs):
        publish(*args, **kwargs)
        raise task.PublicationError("connection lost after publication")

    monkeypatch.setattr(task, "commit_and_push", ambiguous)
    with pytest.raises(task.PublicationError):
        task.checkpoint(world.author, SLUG)
    monkeypatch.setattr(task, "commit_and_push", publish)

    task.checkpoint(world.author, SLUG)

    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    assert journal.count("**Checkpoint**") == 1
    assert journal.count("still open") == 1
    assert not progress.exists()


def test_checkpoint_retry_does_not_consume_changed_progress(
    world: World, monkeypatch,
) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "First publication.\n")
    publish = task.commit_and_push

    def ambiguous(*args, **kwargs):
        publish(*args, **kwargs)
        raise task.PublicationError("connection lost after publication")

    monkeypatch.setattr(task, "commit_and_push", ambiguous)
    with pytest.raises(task.PublicationError):
        task.checkpoint(world.author, SLUG)
    write(progress, "New progress after the uncertain result.\n")

    monkeypatch.setattr(task, "commit_and_push", publish)
    task.checkpoint(world.author, SLUG)

    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    assert journal.count("**Checkpoint**") == 2
    assert "First publication." in journal
    assert "New progress after the uncertain result." in journal
    assert not progress.exists()


def test_checkpoint_treats_id_like_comment_as_free_form_progress(world: World) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    comment = f"<!-- nk-checkpoint-id: {'a' * 32} {'b' * 64} -->"
    write(progress, f"{comment}\n\nThis is authored content.\n")

    task.checkpoint(world.author, SLUG)

    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    assert comment in journal
    assert "This is authored content." in journal


def test_checkpoint_fast_forwards_to_concurrent_descendant(world: World) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Publish before the concurrent update.\n")
    bare = Path(git(world.author, "remote", "get-url", "origin").stdout.strip())
    advance_remote_after_push(bare, f"refs/heads/{world.control_branch}")

    task.checkpoint(world.author, SLUG)

    assert git(world.author, "rev-parse", "HEAD").stdout == git(
        world.author, "rev-parse", f"origin/{world.control_branch}"
    ).stdout


def test_checkpoint_numbers_ignore_fenced_entry_examples(world: World) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(
        progress,
        "Checked the parser.\n\n```markdown\n"
        "## Entry 9999\n\n**Checkpoint**\n```\n",
    )

    task.checkpoint(world.author, SLUG)

    assert task.checkpoint_numbers(world.author, SLUG) == [1]


def test_checkpoint_accepts_thematic_break_after_fenced_block(world: World) -> None:
    claim(world, world.author)
    progress = world.author / "scratch" / SLUG / "progress.md"
    write(progress, "Paragraph\n```\ncode\n```\n---\n")

    task.checkpoint(world.author, SLUG)

    assert "Paragraph" in (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()


@pytest.mark.parametrize(
    "progress", [None, "", "# Heading\n", "Heading\n=======\n", "Heading\n-------\n"]
)
def test_checkpoint_rejects_invalid_progress_without_mutation(
    world: World, progress: str | None,
) -> None:
    claim(world, world.author)
    path = world.author / "scratch" / SLUG / "progress.md"
    if progress is not None:
        write(path, progress)
    before = git(world.author, "rev-parse", "HEAD").stdout

    with pytest.raises(task.CoordinationError):
        task.checkpoint(world.author, SLUG)

    if progress is None:
        assert not path.exists()
    else:
        assert path.read_text() == progress
    assert git(world.author, "rev-parse", "HEAD").stdout == before
    assert git(world.author, "status", "--short").stdout == ""


def test_provisioned_identity_is_shared_by_scheduler_and_interactive_claims(
    world: World, monkeypatch,
) -> None:
    write(world.author / ".workspace/nk/ownership.json", json.dumps({
        "cluster": "work", "node": "node", "workspace": "configured",
        "path": str(world.author),
    }))
    monkeypatch.setenv("NK_WORKSPACE_OWNER", "configured@node")
    scheduled = task.claim(world.author, SLUG, emit=False)

    monkeypatch.delenv("NK_WORKSPACE_OWNER")
    interactive = task.claim(world.author, SLUG, emit=False)

    assert interactive == {
        "status": "resumed", "slug": SLUG,
        "claim_id": scheduled["claim_id"],
    }
    assert task.owner(world.author) == "configured@node"


def handoff(world: World, *repositories: str) -> None:
    args = [item for repo in repositories for item in ("--repository", repo)]
    result = world.coordinate(world.author, "submit", "--slug", SLUG, *args)
    assert result.stdout.startswith("SUBMITTED\t")


def test_claim_freezes_spec_and_rejects_new_candidate_repository(world: World) -> None:
    manifest_path = world.author / "scratch" / SLUG / "task.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repositories"] = ["group/dependency"]
    write(manifest_path, json.dumps(manifest))
    spec_sha = commit(world.author, "declare task repositories")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    for repository in world.child_bares:
        world.make_candidate(world.author, repository, "candidate\n")

    claim(world, world.author)

    claim_data = json.loads(
        (world.author / "scratch" / SLUG / "claim.json").read_text()
    )
    assert claim_data["spec_sha"] == spec_sha
    assert claim_data["repositories"] == ["group/dependency"]
    result = world.coordinate(
        world.author,
        "submit",
        "--slug", SLUG,
        "--repository", "group/dependency",
        "--repository", "group/consumer",
        check=False,
    )
    assert result.returncode == 1
    assert "outside the claim" in result.stderr


def test_candidate_submission_preserves_active_claim_contract(world: World) -> None:
    manifest_path = world.author / "scratch" / SLUG / "task.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["repositories"] = ["group/dependency"]
    write(manifest_path, json.dumps(manifest))
    spec_sha = commit(world.author, "declare task repositories")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    world.make_candidate(world.author, "group/dependency", "candidate\n")
    claim(world, world.author)
    handoff(world, "group/dependency")
    active_claim = json.loads(
        (world.author / "scratch" / SLUG / "claim.json").read_text()
    )
    assert active_claim["spec_sha"] == spec_sha
    assert active_claim["repositories"] == ["group/dependency"]
    assert SLUG in todo_section(world.author, "Authoring")


def add_second_review_task(world: World, author_owner: str) -> None:
    repository = "group/dependency"
    repo = world.author / repository
    git(repo, "switch", "-C", f"candidate/{SECOND_SLUG}", "origin/main")
    write(repo / "value.txt", "second candidate\n")
    second_sha = commit(repo, "second candidate")
    git(repo, "push", "origin", f"HEAD:refs/heads/candidate/{SECOND_SLUG}")
    base_sha = git(repo, "merge-base", "origin/main", second_sha).stdout.strip()
    write(
        world.author / "TODO.md",
        todo_entries([(SLUG, "Authoring"), (SECOND_SLUG, "Authoring")]),
    )
    write(world.author / "scratch" / SECOND_SLUG / "README.md", "# Second task\n")
    write(
        world.author / "scratch" / SECOND_SLUG / "task.json",
        json.dumps({"dependencies": [], "capabilities": {}, "resources": {}}),
    )
    write(
        world.author / "scratch" / SECOND_SLUG / "candidate.json",
        json.dumps(
            {
                "slug": SECOND_SLUG,
                "author_owner": author_owner,
                "repositories": [
                    {
                        "path": repository,
                        "target_ref": "refs/heads/main",
                        "base_sha": base_sha,
                        "candidate_sha": second_sha,
                    }
                ],
            }
        ),
    )
    commit(world.author, "add second review task")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")


def approve_candidate(world: World) -> None:
    # Review is procedural inside the author goal loop; the CLI stores no verdict.
    pass


def validate_task_plan(world: World, verdict: str = "pass") -> None:
    records = world.author.parent / "records.json"
    records.write_text(
        json.dumps(
            [
                {
                    "name": "tests",
                    "repository": "group/dependency",
                    "argv": ["pytest", "-q"],
                    "exit_status": 0,
                    "started_at": "2026-07-02T10:00:00Z",
                    "ended_at": "2026-07-02T10:00:01Z",
                    "artifacts": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    result = world.coordinate(
        world.author,
        "record-validation",
        "--slug",
        SLUG,
        "--task-plan-records",
        str(records),
        "--verdict",
        verdict,
    )
    assert result.stdout.startswith("VALIDATED\t")


def prepare_repositories(world: World, *repositories: str) -> None:
    for repository in repositories:
        world.make_candidate(world.author, repository, f"candidate {repository}\n")
    claim(world, world.author)
    handoff(world, *repositories)
    approve_candidate(world)


def test_two_repository_happy_path_integrates_exact_merge_and_cleans_refs(world: World) -> None:
    candidate_shas = {
        repo: world.make_candidate(world.author, repo, f"candidate {repo}\n")
        for repo in world.child_bares
    }
    claim(world, world.author)
    handoff(world, *world.child_bares)
    approve_candidate(world)

    revision_before_validation = git(world.author, "rev-parse", "HEAD").stdout.strip()
    validate_task_plan(world)
    validation = json.loads(
        (world.author / "scratch" / SLUG / "validation.json").read_text()
    )
    assert validation["definition"]["kind"] == "task_plan"
    assert validation["definition"]["task_revision"] == revision_before_validation
    assert validation["definition"]["task_path"] == f"scratch/{SLUG}/README.md"
    assert validation["verdict"] == "pass"
    assert "merge_digest" not in validation

    result = world.coordinate(world.author, "complete", "--slug", SLUG)
    assert result.stdout.startswith("COMPLETED\t")
    merge = json.loads((world.author / "scratch" / SLUG / "merge.json").read_text())
    assert [item["path"] for item in merge["repositories"]] == list(world.child_bares)
    for item in merge["repositories"]:
        repo = world.author / item["path"]
        parents = git(repo, "show", "-s", "--format=%P", item["merge_sha"]).stdout.split()
        assert parents == [item["target_sha"], candidate_shas[item["path"]]]
    assert not (world.author / "scratch" / SLUG / "integration.json").exists()
    for item in merge["repositories"]:
        repo = world.author / item["path"]
        assert remote_sha(repo, item["target_ref"]) == item["merge_sha"]
        assert remote_sha(repo, f"refs/heads/candidate/{SLUG}") is None
        assert git(repo, "branch", "--show-current").stdout.strip() == "main"
        assert git(repo, "rev-parse", "HEAD").stdout.strip() == item["merge_sha"]
    assert SLUG in todo_section(world.author, "Done")
    assert not (world.author / "scratch" / SLUG / "claim.json").exists()


def test_completion_preserves_unpublished_detached_child(world: World) -> None:
    repository = "group/dependency"
    prepare_repositories(world, repository)
    validate_task_plan(world)
    repo = world.author / repository
    write(repo / "unpublished.txt", "keep me\n")
    commit(repo, "unpublished review residue")
    unpublished = git(repo, "rev-parse", "HEAD").stdout.strip()

    result = world.coordinate(world.author, "complete", "--slug", SLUG, check=False)

    assert result.returncode == 1
    assert "unpublished work" in result.stderr
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == unpublished
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()


def test_completion_reconciles_unrelated_queue_race_after_child_push(
    world: World,
) -> None:
    repository = "group/dependency"
    prepare_repositories(world, repository)
    validate_task_plan(world)
    control = Path(git(world.author, "remote", "get-url", "origin").stdout.strip())
    advance_control_after_child_push(
        world.child_bares[repository], control, f"refs/heads/{world.control_branch}"
    )

    result = world.coordinate(world.author, "complete", "--slug", SLUG)

    assert result.stdout == f"COMPLETED\t{SLUG}\tDone\n"
    assert SLUG in todo_section(world.author, "Done")
    assert not (world.author / "scratch" / SLUG / "integration.json").exists()
    assert git(world.author, "status", "--short").stdout == ""
    assert (
        git(world.author, "rev-parse", "HEAD").stdout.strip()
        == git(world.author, "rev-parse", "origin/queue").stdout.strip()
    )


def test_completion_route_rejects_same_task_change(world: World) -> None:
    repository = "group/dependency"
    prepare_repositories(world, repository)
    validate_task_plan(world)
    control = task.resolve_default_branch(world.author)
    expected_sha = git(world.author, "rev-parse", "HEAD").stdout.strip()
    expected_task_tree = git(
        world.author, "rev-parse", f"{expected_sha}:scratch/{SLUG}"
    ).stdout.strip()
    world.sync(world.author)
    manifest = world.author / "scratch" / SLUG / "task.json"
    value = json.loads(manifest.read_text())
    value["resources"] = {"gpu": 0}
    manifest.write_text(json.dumps(value, indent=2) + "\n")
    commit(world.author, "change task during completion")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")

    with pytest.raises(task.PublicationError, match="task changed"):
        task.publish_completion(
            world.author,
            control,
            expected_task_tree,
            SLUG,
        )

    world.sync(world.author)
    assert SLUG in todo_section(world.author, "Authoring")
    assert json.loads(
        (world.author / "scratch" / SLUG / "task.json").read_text()
    )["resources"] == {"gpu": 0}


def todo_section(workspace: Path, bucket: str) -> str:
    text = (workspace / "TODO.md").read_text()
    return text.split(f"## {bucket}\n", 1)[1].split("\n## ", 1)[0]


def test_failed_validation_preserves_review_for_explicit_route(world: World) -> None:
    prepare_repositories(world, "group/dependency")

    validate_task_plan(world, "unavailable")

    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()
    assert (world.author / "scratch" / SLUG / "validation.json").exists()


def test_author_cannot_return_claimed_task_to_ready(world: World) -> None:
    prepare_repositories(world, "group/dependency")

    validate_task_plan(world, "unavailable")
    task_dir = world.author / "scratch" / SLUG
    result = world.coordinate(world.author, "ready", SLUG, check=False)

    assert result.returncode == 1
    assert "cannot become Ready from Authoring" in result.stderr
    assert SLUG in todo_section(world.author, "Authoring")
    assert (task_dir / "claim.json").exists()
    assert (task_dir / "README.md").read_text() == "# Test task\n"
    assert (task_dir / "validation.json").exists()
    assert not (task_dir / "merge.json").exists()


def test_validation_without_next_attempt_routes_to_blocked_explicitly(
    world: World,
) -> None:
    prepare_repositories(world, "group/dependency")
    validate_task_plan(world, "unavailable")
    task_dir = world.author / "scratch" / SLUG
    write(task_dir / "blocker.md", "Operator must restore external access.\n")

    world.coordinate(world.author, "block", SLUG)

    assert SLUG in todo_section(world.author, "Blocked")
    assert not (task_dir / "claim.json").exists()
    assert (task_dir / "validation.json").exists()
    assert (task_dir / "blocker.md").exists()


def test_repaired_handoff_removes_stale_evidence(world: World) -> None:
    world.make_candidate(world.author, "group/dependency", "candidate one\n")
    claim(world, world.author)
    handoff(world, "group/dependency")
    approve_candidate(world)
    validate_task_plan(world)
    review_dir = world.author / "scratch" / SLUG
    assert (review_dir / "validation.json").exists()
    assert not (review_dir / "merge.json").exists()

    repo = world.author / "group/dependency"
    git(repo, "switch", f"candidate/{SLUG}")
    write(repo / "value.txt", "candidate two\n")
    commit(repo, "repair candidate")
    git(repo, "push", "origin", f"HEAD:refs/heads/candidate/{SLUG}")
    handoff(world, "group/dependency")
    for name in ("merge.json", "validation.json"):
        assert not (world.author / "scratch" / SLUG / name).exists()


def test_completion_keeps_merge_conflict_claimed_in_authoring(world: World) -> None:
    world.make_candidate(world.author, "group/dependency", "candidate\n")
    target = world.author / "group/dependency"
    git(target, "switch", "main")
    write(target / "value.txt", "target\n")
    commit(target, "conflicting target")
    git(target, "push", "origin", "main")
    claim(world, world.author)
    handoff(world, "group/dependency")
    validate_task_plan(world)
    result = world.coordinate(world.author, "complete", "--slug", SLUG)
    assert result.stdout.startswith(f"CONFLICT\t{SLUG}\tAuthoring\t")
    assert not (world.author / "scratch" / SLUG / "merge.json").exists()
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()


def test_completion_preserves_review_for_operational_merge_failure(world: World) -> None:
    prepare_repositories(world, "group/dependency")
    validate_task_plan(world)
    repo = world.author / "group/dependency"
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "gpg.program", "definitely-missing-gpg")

    result = world.coordinate(
        world.author, "complete", "--slug", SLUG, check=False,
    )

    assert result.returncode == 1
    assert "candidate merge failed" in result.stderr
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()


def test_changed_candidate_is_rejected_before_completion(world: World) -> None:
    world.make_candidate(world.author, "group/dependency", "candidate\n")
    claim(world, world.author)
    handoff(world, "group/dependency")
    approve_candidate(world)
    validate_task_plan(world)

    repo = world.author / "group/dependency"
    git(repo, "switch", f"candidate/{SLUG}")
    write(repo / "other.txt", "changed\n")
    commit(repo, "move candidate")
    git(repo, "push", "origin", f"HEAD:refs/heads/candidate/{SLUG}")

    result = world.coordinate(world.author, "complete", "--slug", SLUG, check=False)
    assert result.returncode == 1
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()
    assert remote_sha(repo, f"refs/heads/candidate/{SLUG}") is not None


def test_completion_absorbs_target_movement_without_revalidation(world: World) -> None:
    prepare_repositories(world, "group/dependency")
    validate_task_plan(world)
    validation_before = (
        world.author / "scratch" / SLUG / "validation.json"
    ).read_bytes()
    repo = world.author / "group/dependency"
    git(repo, "switch", "main")
    write(repo / "other.txt", "target movement\n")
    concurrent = commit(repo, "advance target")
    git(repo, "push", "origin", f"{concurrent}:refs/heads/concurrent")
    move_remote_ref_after_push_once(
        world.child_bares["group/dependency"], "refs/heads/main", concurrent,
    )

    result = world.coordinate(world.author, "complete", "--slug", SLUG)

    assert result.stdout == f"COMPLETED\t{SLUG}\tDone\n"
    assert (
        world.author / "scratch" / SLUG / "validation.json"
    ).read_bytes() == validation_before


def test_cancel_publishes_cancelled_without_touching_repository_refs(world: World) -> None:
    repo = world.author / "group/dependency"
    world.make_candidate(world.author, "group/dependency", "candidate\n")
    git(repo, "branch", "unrelated", "origin/main")
    git(repo, "push", "origin", "unrelated")
    claim(world, world.author)
    cancellation = world.author / "scratch" / SLUG / "cancellation.md"
    write(cancellation, "The work is intentionally obsolete.\n")
    result = world.coordinate(
        world.author,
        "cancel",
        SLUG,
    )
    assert result.stdout == f"CANCELLED\t{SLUG}\n"
    assert remote_sha(repo, f"refs/heads/candidate/{SLUG}") is not None
    assert remote_sha(repo, "refs/heads/unrelated") is not None
    assert SLUG in todo_section(world.author, "Cancelled")
    assert not (world.author / "scratch" / SLUG / "claim.json").exists()
    assert not cancellation.exists()
    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    assert "**Cancellation**" in journal
    assert "The work is intentionally obsolete." in journal
    for name in ("candidate.json", "merge.json", "validation.json"):
        assert not (world.author / "scratch" / SLUG / name).exists()


@pytest.mark.parametrize("source", ["Ready", "Backlog"])
def test_cancel_from_unclaimed_state_records_reason(
    world: World, source: str,
) -> None:
    if source == "Backlog":
        task.move_todo(world.author, SLUG, "Ready", "Backlog")
        commit(world.author, "Move task to backlog")
        git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    directory = world.author / "scratch" / SLUG
    write(directory / "cancellation.md", f"Cancel from {source}.\n")

    result = world.coordinate(world.author, "cancel", SLUG)

    assert result.stdout == f"CANCELLED\t{SLUG}\n"
    assert SLUG in todo_section(world.author, "Cancelled")
    assert f"Cancel from {source}." in (directory / "JOURNAL.md").read_text()


def test_cancel_preserves_authoring_dependent_claim_for_its_next_turn(
    world: World,
) -> None:
    write(
        world.author / "TODO.md",
        todo_entries([(SLUG, "Ready"), (SECOND_SLUG, "Authoring")]),
    )
    dependent = world.author / "scratch" / SECOND_SLUG
    write(dependent / "README.md", "# Dependent task\n")
    write(
        dependent / "task.json",
        json.dumps({
            "dependencies": [SLUG], "capabilities": {}, "resources": {},
        }),
    )
    claim_data = {
        "owner": task.owner(world.author),
        "claim_id": "dependent-claim",
    }
    write(dependent / "claim.json", json.dumps(claim_data))
    commit(world.author, "add claimed dependent")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    cancellation = world.author / "scratch" / SLUG / "cancellation.md"
    write(cancellation, "The prerequisite is obsolete.\n")

    result = world.coordinate(world.author, "cancel", SLUG)

    assert result.stdout == f"CANCELLED\t{SLUG}\n"
    assert SLUG in todo_section(world.author, "Cancelled")
    assert SECOND_SLUG in todo_section(world.author, "Authoring")
    assert json.loads((dependent / "claim.json").read_text()) == claim_data
    assert not (dependent / "blocker.md").exists()


@pytest.mark.parametrize("value", [None, "", "# Invalid heading\n"])
def test_cancel_requires_valid_cancellation_without_mutation(
    world: World, value: str | None,
) -> None:
    cancellation = world.author / "scratch" / SLUG / "cancellation.md"
    if value is not None:
        write(cancellation, value)
    before = git(world.author, "rev-parse", "HEAD").stdout

    result = world.coordinate(world.author, "cancel", SLUG, check=False)

    assert result.returncode == 1
    assert SLUG in todo_section(world.author, "Ready")
    assert git(world.author, "rev-parse", "HEAD").stdout == before
    assert git(world.author, "status", "--short").stdout == ""
    if value is not None:
        assert cancellation.read_text() == value


def test_cancel_rejects_unrelated_local_changes(world: World) -> None:
    cancellation = world.author / "scratch" / SLUG / "cancellation.md"
    write(cancellation, "Cancel this task.\n")
    write(world.author / "unrelated.txt", "keep\n")

    result = world.coordinate(world.author, "cancel", SLUG, check=False)

    assert result.returncode == 1
    assert "queue checkout is dirty" in result.stderr
    assert cancellation.read_text() == "Cancel this task.\n"
    assert SLUG in todo_section(world.author, "Ready")


def test_cancel_preserves_terminal_rejection(world: World) -> None:
    task.move_todo(world.author, SLUG, "Ready", "Cancelled")
    commit(world.author, "Cancel task out of band")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    cancellation = world.author / "scratch" / SLUG / "cancellation.md"

    result = world.coordinate(world.author, "cancel", SLUG, check=False)

    assert result.returncode == 1
    assert "terminal task cannot be cancelled" in result.stderr
    assert not cancellation.exists()


def test_cancel_blocked_records_reason_and_existing_blocker(world: World) -> None:
    directory = world.author / "scratch" / SLUG
    write(directory / "blocker.md", "An external dependency is unavailable.\n")
    world.coordinate(world.author, "block", SLUG)
    write(directory / "cancellation.md", "The dependent work is no longer needed.\n")

    result = world.coordinate(world.author, "cancel", SLUG)

    assert result.stdout == f"CANCELLED\t{SLUG}\n"
    journal = (directory / "JOURNAL.md").read_text()
    assert "**Cancellation**" in journal
    assert "The dependent work is no longer needed." in journal
    assert "### Existing blocker" in journal
    assert "An external dependency is unavailable." in journal
    assert not (directory / "blocker.md").exists()
    assert not (directory / "resolution.md").exists()


def test_cancel_push_failure_preserves_clean_recoverable_state(
    world: World, monkeypatch,
) -> None:
    claim_id = claim(world, world.author).strip()
    directory = world.author / "scratch" / SLUG
    cancellation = directory / "cancellation.md"
    write(cancellation, "Keep this cancellation reason.\n")
    before = git(world.author, "rev-parse", "HEAD").stdout
    monkeypatch.setattr(
        task, "push_control_ref",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "rejected"),
    )

    with pytest.raises(task.PublicationError, match="cancellation push failed"):
        task.cancel_task(world.author, SLUG)

    assert cancellation.read_text() == "Keep this cancellation reason.\n"
    assert json.loads((directory / "claim.json").read_text())["claim_id"] == claim_id
    assert SLUG in todo_section(world.author, "Authoring")
    assert git(world.author, "rev-parse", "HEAD").stdout == before
    assert git(world.author, "status", "--short").stdout == ""


def test_cancel_lease_exhaustion_synchronizes_clean_checkout(
    world: World, monkeypatch,
) -> None:
    claim_id = claim(world, world.author).strip()
    directory = world.author / "scratch" / SLUG
    cancellation = directory / "cancellation.md"
    write(cancellation, "Keep this reason through lease races.\n")
    bare = Path(git(world.author, "remote", "get-url", "origin").stdout.strip())
    publish = task.push_control_ref

    def lose_lease(repo, control, expected_sha, source="HEAD"):
        current = git(bare, "rev-parse", control.ref).stdout.strip()
        tree = git(bare, "rev-parse", f"{current}^{{tree}}").stdout.strip()
        advanced = git(
            bare, "commit-tree", tree, "-p", current,
            "-m", "concurrent control change",
        ).stdout.strip()
        git(bare, "update-ref", control.ref, advanced, current)
        return publish(repo, control, expected_sha, source)

    monkeypatch.setattr(task, "push_control_ref", lose_lease)

    with pytest.raises(task.PublicationError, match="did not converge"):
        task.cancel_task(world.author, SLUG)

    assert cancellation.read_text() == "Keep this reason through lease races.\n"
    assert json.loads((directory / "claim.json").read_text())["claim_id"] == claim_id
    assert SLUG in todo_section(world.author, "Authoring")
    assert git(world.author, "status", "--short").stdout == ""
    assert git(world.author, "rev-parse", "HEAD").stdout == git(
        world.author, "rev-parse", f"origin/{world.control_branch}"
    ).stdout


def test_cancel_retry_reconciles_publication_interruption(
    world: World, monkeypatch,
) -> None:
    claim(world, world.author)
    cancellation = world.author / "scratch" / SLUG / "cancellation.md"
    write(cancellation, "Publish this cancellation once.\n")
    fetch = task.fetch_ref
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise task.PublicationError("connection lost after publication")
        return fetch(*args, **kwargs)

    monkeypatch.setattr(task, "fetch_ref", interrupted)
    with pytest.raises(task.PublicationError, match="connection lost"):
        task.cancel_task(world.author, SLUG)

    assert cancellation.exists()
    assert SLUG in todo_section(world.author, "Authoring")
    monkeypatch.setattr(task, "fetch_ref", fetch)

    task.cancel_task(world.author, SLUG)

    assert SLUG in todo_section(world.author, "Cancelled")
    assert not cancellation.exists()
    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    assert journal.count("**Cancellation**") == 1
    assert journal.count("Publish this cancellation once.") == 1


def test_cancel_retry_reconciles_interruption_after_checkout_sync(
    world: World, monkeypatch,
) -> None:
    claim(world, world.author)
    cancellation = world.author / "scratch" / SLUG / "cancellation.md"
    write(cancellation, "Finish cleanup after synchronization.\n")
    synchronize = task.synchronize_checkout
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        synchronize(*args, **kwargs)
        if calls == 3:
            raise task.PublicationError("interrupted after checkout synchronization")

    monkeypatch.setattr(task, "synchronize_checkout", interrupted)
    with pytest.raises(task.PublicationError, match="after checkout synchronization"):
        task.cancel_task(world.author, SLUG)

    assert cancellation.exists()
    assert SLUG in todo_section(world.author, "Cancelled")
    monkeypatch.setattr(task, "synchronize_checkout", synchronize)

    task.cancel_task(world.author, SLUG)

    assert SLUG in todo_section(world.author, "Cancelled")
    assert not cancellation.exists()
    journal = (world.author / "scratch" / SLUG / "JOURNAL.md").read_text()
    assert journal.count("**Cancellation**") == 1
    assert journal.count("Finish cleanup after synchronization.") == 1


def test_claim_refuses_non_control_branch(world: World) -> None:
    git(world.author, "switch", "-c", "candidate/control-change")
    claim_result = world.coordinate(
        world.author, "claim", "--slug", SLUG, check=False
    )
    assert claim_result.returncode == 1
    assert git(world.author, "branch", "--show-current").stdout.strip() == "candidate/control-change"
    assert remote_sha(world.author, f"refs/heads/{world.control_branch}") == remote_sha(
        world.author, f"refs/heads/{world.control_branch}"
    )


def child_meta(world: World) -> None:
    write(
        world.author / ".meta",
        json.dumps({
            "projects": {
                relative: str(remote)
                for relative, remote in world.child_bares.items()
            }
        }),
    )


def test_prepare_children_parks_clean_pushed_branch(world: World) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    git(repo, "switch", "-c", "candidate/parked")
    write(repo / "candidate.txt", "preserved remotely\n")
    candidate = commit(repo, "parked candidate")
    git(repo, "push", "-u", "origin", "candidate/parked")

    task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == remote_sha(
        repo, "refs/heads/main"
    )
    assert remote_sha(repo, "refs/heads/candidate/parked") == candidate


def test_prepare_children_parks_branch_advanced_remotely(world: World) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    branch = "candidate/advanced"
    ref = f"refs/heads/{branch}"
    git(repo, "switch", "-c", branch)
    write(repo / "candidate.txt", "preserved remotely\n")
    local = commit(repo, "advanced candidate")
    advance_remote_after_push(world.child_bares["group/dependency"], ref)
    git(repo, "push", "-u", "origin", branch)

    assert remote_sha(repo, ref) != local

    task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == "main"


def test_prepare_children_parks_unpublished_branch_at_remote_default(
    world: World,
) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    git(repo, "switch", "-c", "candidate/already-merged")

    task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == "main"


def test_prepare_children_fetches_repositories_once_in_parallel(
    world: World, monkeypatch
) -> None:
    child_meta(world)
    real_git = task.git
    barrier = threading.Barrier(len(world.child_bares))
    fetched = []
    lock = threading.Lock()

    def observed_git(repo, *arguments, **kwargs):
        if arguments and arguments[0] == "fetch":
            with lock:
                fetched.append(repo)
            barrier.wait(timeout=5)
        if arguments and arguments[0] == "ls-remote":
            raise AssertionError("child preparation used an extra remote query")
        return real_git(repo, *arguments, **kwargs)

    monkeypatch.setattr(task, "git", observed_git)

    task.prepare_children(world.author)

    assert sorted(fetched) == sorted(
        world.author / relative for relative in world.child_bares
    )


def test_prepare_children_preserves_empty_repository(world: World, tmp_path: Path) -> None:
    remote = tmp_path / "empty.git"
    git(tmp_path, "init", "--bare", str(remote))
    world.child_bares["group/empty"] = remote
    git(world.author, "clone", str(remote), "group/empty")
    child_meta(world)

    task.prepare_children(world.author)

    assert git(
        world.author / "group/empty", "rev-parse", "--verify", "HEAD", check=False
    ).returncode != 0


def test_prepare_children_preserves_unpushed_branch(world: World) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    git(repo, "switch", "-c", "candidate/unpushed")
    write(repo / "candidate.txt", "local only\n")
    candidate = commit(repo, "unpushed candidate")

    with pytest.raises(task.CoordinationError, match="not fully pushed"):
        task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == "candidate/unpushed"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == candidate


def test_prepare_children_prunes_deleted_remote_branch(world: World) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    git(repo, "switch", "-c", "candidate/deleted")
    write(repo / "candidate.txt", "once pushed\n")
    candidate = commit(repo, "deleted candidate")
    git(repo, "push", "-u", "origin", "candidate/deleted")
    git(
        world.child_bares["group/dependency"], "update-ref", "-d",
        "refs/heads/candidate/deleted",
    )

    with pytest.raises(task.CoordinationError, match="not fully pushed"):
        task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == "candidate/deleted"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == candidate


def test_prepare_children_follows_changed_remote_default(world: World) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    git(repo, "switch", "-c", "trunk")
    write(repo / "trunk.txt", "new default\n")
    trunk = commit(repo, "new default")
    git(repo, "push", "origin", "trunk")
    git(world.child_bares["group/dependency"], "symbolic-ref", "HEAD", "refs/heads/trunk")
    git(repo, "switch", "main")

    task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == "trunk"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == trunk


def test_prepare_children_resolves_ambiguous_changed_default(world: World) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "branch", "trunk", head)
    git(repo, "push", "origin", "trunk")
    git(world.child_bares["group/dependency"], "symbolic-ref", "HEAD", "refs/heads/trunk")

    task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == "trunk"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head


def test_prepare_children_preserves_dirty_branch(world: World) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    git(repo, "switch", "-c", "candidate/dirty")
    write(repo / "value.txt", "uncommitted\n")

    with pytest.raises(task.CoordinationError, match="tracked changes"):
        task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == "candidate/dirty"
    assert (repo / "value.txt").read_text() == "uncommitted\n"


def test_prepare_children_preserves_detached_checkout(world: World) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "--detach", head)

    with pytest.raises(task.CoordinationError, match="detached"):
        task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == ""
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head


def test_prepare_children_preserves_diverged_default_branch(world: World) -> None:
    child_meta(world)
    repo = world.author / "group/dependency"
    write(repo / "local.txt", "local default commit\n")
    commit(repo, "diverge default")
    git(repo, "switch", "-c", "candidate/pushed")
    git(repo, "push", "-u", "origin", "candidate/pushed")

    with pytest.raises(task.CoordinationError, match="diverged"):
        task.prepare_children(world.author)

    assert git(repo, "branch", "--show-current").stdout.strip() == "candidate/pushed"


def test_prepare_children_validates_all_children_before_parking(world: World) -> None:
    child_meta(world)
    first = world.author / "group/dependency"
    git(first, "switch", "-c", "candidate/parked")
    write(first / "candidate.txt", "preserved remotely\n")
    commit(first, "parked candidate")
    git(first, "push", "-u", "origin", "candidate/parked")
    second = world.author / "group/consumer"
    git(second, "switch", "-c", "candidate/dirty")
    write(second / "value.txt", "uncommitted\n")

    with pytest.raises(task.CoordinationError, match="tracked changes"):
        task.prepare_children(world.author)

    assert git(first, "branch", "--show-current").stdout.strip() == "candidate/parked"
    assert git(second, "branch", "--show-current").stdout.strip() == "candidate/dirty"


def test_empty_claim_does_not_require_clean_or_synchronized_checkout(world: World) -> None:
    write(world.author / "TODO.md", todo("Backlog"))
    commit(world.author, "move task out of ready")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    write(world.author / "local.txt", "dirty\n")

    result = world.coordinate(world.author, "claim")

    fields = result.stdout.strip().split("\t")
    assert fields[0] == "EMPTY"
    assert len(fields) == 1
    assert (world.author / "local.txt").exists()


def test_resume_cleans_registered_children(world: World) -> None:
    child_meta(world)
    commit(world.author, "register children")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    claim(world, world.author)
    child = world.author / "group/dependency"
    write(child / "dirty.txt", "keep\n")

    result = world.coordinate(world.author, "claim")

    fields = result.stdout.strip().split("\t")
    assert fields[:2] == ["RESUMED", SLUG]
    assert len(fields) == 3
    assert not (child / "dirty.txt").exists()


@pytest.mark.parametrize("command", ["inspect", "review-release"])
def test_removed_commands_are_rejected(world: World, command: str) -> None:
    result = world.coordinate(world.author, command, check=False)
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_author_skips_unresolved_dependency_and_manual_slug_cannot_bypass(
    world: World,
) -> None:
    write(
        world.author / "TODO.md",
        todo_entries([(SLUG, "Ready"), (SECOND_SLUG, "Ready")]),
    )
    write(world.author / "scratch" / SECOND_SLUG / "README.md", "# Second task\n")
    write(
        world.author / "scratch" / SLUG / "task.json",
        json.dumps({"dependencies": [SECOND_SLUG], "capabilities": {}, "resources": {}}),
    )
    write(
        world.author / "scratch" / SECOND_SLUG / "task.json",
        json.dumps({"dependencies": [], "capabilities": {}, "resources": {}}),
    )
    commit(world.author, "add dependent queue")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")

    manual = world.coordinate(
        world.author,
        "claim",
        "--slug",
        SLUG,
        check=False,
    )
    assert manual.returncode == 1
    assert "unresolved dependencies" in manual.stderr
    result = world.coordinate(world.author, "claim")
    assert result.stdout.split("\t", 2)[:2] == ["CLAIMED", SECOND_SLUG]


def test_new_claim_clones_missing_meta_child(world: World) -> None:
    missing = bare_repo(world.author.parent, "missing-child", {"value.txt": "base\n"})
    write(
        world.author / ".meta",
        json.dumps({"projects": {"group/missing": str(missing)}}),
    )
    commit(world.author, "register missing child")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")

    claim(world, world.author)

    assert git(world.author / "group/missing", "status", "--short").stdout == ""


def test_new_claim_cleans_untracked_and_ignored_registered_child(world: World) -> None:
    write(
        world.author / ".meta",
        json.dumps(
            {"projects": {"group/dependency": str(world.child_bares["group/dependency"])}}
        ),
    )
    commit(world.author, "register child")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    child = world.author / "group/dependency"
    write(child / "dirty.txt", "discard\n")
    write(child / ".git" / "info" / "exclude", "build/\n")
    write(child / "build" / "cache", "discard\n")

    result = world.coordinate(world.author, "claim")

    assert result.stdout.startswith(f"CLAIMED\t{SLUG}\t")
    assert not (child / "dirty.txt").exists()
    assert not (child / "build").exists()


def test_non_racing_claim_push_rejection_fails_closed(world: World) -> None:
    reject_remote_ref(
        Path(git(world.author, "remote", "get-url", "origin").stdout.strip()),
        f"refs/heads/{world.control_branch}",
    )

    result = world.coordinate(world.author, "claim", check=False)

    assert result.returncode == 1
    assert "coordination push failed" in result.stderr
    assert not (world.author / "scratch" / SLUG / "claim.json").exists()


def test_claim_hides_commit_when_remote_advances_after_publication(
    world: World,
) -> None:
    bare = Path(git(world.author, "remote", "get-url", "origin").stdout.strip())
    advance_remote_after_push(bare, f"refs/heads/{world.control_branch}")

    result = world.coordinate(world.author, "claim")

    assert len(result.stdout.strip().split("\t")) == 3
    observed = git(world.author, "rev-parse", "HEAD").stdout.strip()
    assert observed == git(world.author, "rev-parse", "origin/queue").stdout.strip()


def test_concurrent_default_claims_do_not_duplicate_ownership(world: World) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Workflow Test",
            "GIT_AUTHOR_EMAIL": "workflow@example.invalid",
            "GIT_COMMITTER_NAME": "Workflow Test",
            "GIT_COMMITTER_EMAIL": "workflow@example.invalid",
        }
    )
    commands = [
        [
            sys.executable,
            str(HELPER),
            "task",
            "claim",
            "--workspace",
            str(workspace),
        ]
        for workspace in (world.author, world.reviewer)
    ]
    processes = [
        subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for command in commands
    ]
    results = [process.communicate(timeout=10) for process in processes]

    assert [process.returncode for process in processes] == [0, 0]
    outcomes = sorted(stdout.split("\t", 1)[0].strip() for stdout, _ in results)
    assert outcomes == ["CLAIMED", "EMPTY"]
    world.sync(world.author)
    claims = list((world.author / "scratch").glob("*/claim.json"))
    assert len(claims) == 1
    assert remote_sha(world.author, "refs/heads/task-coordination-lock") is None
    assert (world.author / ".workspace" / "task-coordination.lock").exists()
    assert git(world.author, "status", "--short").stdout == ""


def test_scheduler_snapshot_resumes_owned_claim(world: World) -> None:
    claim(world, world.author)
    reference = f"{world.author.name}@localhost"

    result = run(
        sys.executable,
        str(HELPER),
        "scheduler",
        "_snapshot",
        "--workspace",
        str(world.author),
    )

    candidates = json.loads(result.stdout)["candidates"]
    assert candidates[0]["slug"] == SLUG
    assert candidates[0]["workspace"] == reference


def test_scheduler_snapshot_occupies_unresumable_claim_owner(
    world: World,
) -> None:
    claim(world, world.author)
    world.coordinate(world.author, "create", SECOND_SLUG)
    world.coordinate(world.author, "dependency", "add", SLUG, SECOND_SLUG)
    unrelated = "2026-07-05-unrelated-ready"
    world.coordinate(world.author, "create", unrelated)
    write(
        world.author / "scratch" / unrelated / "task.json",
        json.dumps({"dependencies": [], "capabilities": {}, "resources": {}}),
    )
    world.coordinate(world.author, "ready", unrelated)
    reference = f"{world.author.name}@localhost"

    result = run(
        sys.executable,
        str(HELPER),
        "scheduler",
        "_snapshot",
        "--workspace",
        str(world.author),
    )

    snapshot = json.loads(result.stdout)
    assert snapshot["occupied"] == [reference]
    assert [item["slug"] for item in snapshot["candidates"]] == [unrelated]


def test_exact_claim_rejects_different_owned_task(world: World) -> None:
    claim(world, world.author)
    world.coordinate(world.author, "create", SECOND_SLUG)
    write(
        world.author / "scratch" / SECOND_SLUG / "task.json",
        json.dumps({"dependencies": [], "capabilities": {}, "resources": {}}),
    )
    world.coordinate(world.author, "ready", SECOND_SLUG)

    result = world.coordinate(
        world.author,
        "claim",
        "--slug",
        SECOND_SLUG,
        check=False,
    )

    assert result.returncode == 1
    assert f"workspace owns {SLUG}" in result.stderr


def test_complete_rejects_new_unresolved_dependency(world: World) -> None:
    prepare_repositories(world, "group/dependency")
    validate_task_plan(world)
    world.coordinate(world.author, "create", SECOND_SLUG)
    world.coordinate(world.author, "dependency", "add", SLUG, SECOND_SLUG)

    result = world.coordinate(
        world.author, "complete", "--slug", SLUG, check=False
    )

    assert result.returncode == 1
    assert "unresolved dependencies" in result.stderr
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()


def test_completion_cleans_first_temporary_ref_when_second_remote_rejects(world: World) -> None:
    repositories = tuple(world.child_bares)
    for repository in repositories:
        world.make_candidate(world.author, repository, f"candidate {repository}\n")
    claim(world, world.author)
    handoff(world, *repositories)
    validate_task_plan(world)
    reject_remote_ref(
        world.child_bares[repositories[1]], f"refs/heads/validation/{SLUG}"
    )

    result = world.coordinate(world.author, "complete", "--slug", SLUG, check=False)

    assert result.returncode == 1
    for repository in repositories:
        assert remote_sha(
            world.author / repository, f"refs/heads/validation/{SLUG}"
        ) is None
    assert not (world.author / "scratch" / SLUG / "merge.json").exists()
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()


def test_completion_retries_after_partial_target_publication(world: World) -> None:
    repositories = tuple(world.child_bares)
    prepare_repositories(world, *repositories)
    validate_task_plan(world)
    reject_remote_ref(world.child_bares[repositories[1]], "refs/heads/main")

    result = world.coordinate(world.author, "complete", "--slug", SLUG, check=False)

    assert result.returncode == 1
    merge = json.loads(
        (world.author / "scratch" / SLUG / "merge.json").read_text()
    )
    assert remote_sha(
        world.author / repositories[0], merge["repositories"][0]["target_ref"]
    ) == merge["repositories"][0]["merge_sha"]
    assert remote_sha(
        world.author / repositories[1], merge["repositories"][1]["target_ref"]
    ) == merge["repositories"][1]["target_sha"]
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()
    (world.child_bares[repositories[1]] / "hooks" / "pre-receive").unlink()

    repeated = world.coordinate(world.author, "complete", "--slug", SLUG)

    assert repeated.stdout.startswith("COMPLETED\t")
    assert SLUG in todo_section(world.author, "Done")
    assert not (world.author / "scratch" / SLUG / "integration.json").exists()


def test_completion_rejects_candidate_drift_after_partial_publication(
    world: World,
) -> None:
    repositories = tuple(world.child_bares)
    prepare_repositories(world, *repositories)
    validate_task_plan(world)
    reject_remote_ref(world.child_bares[repositories[1]], "refs/heads/main")

    first_attempt = world.coordinate(
        world.author, "complete", "--slug", SLUG, check=False,
    )

    assert first_attempt.returncode == 1
    first = repositories[0]
    candidate = json.loads(
        (world.author / "scratch" / SLUG / "candidate.json").read_text()
    )["repositories"][0]
    assert not git(
        world.author / first,
        "merge-base",
        "--is-ancestor",
        candidate["candidate_sha"],
        remote_sha(world.author / first, candidate["target_ref"]),
        check=False,
    ).returncode
    repo = world.author / first
    git(repo, "switch", f"candidate/{SLUG}")
    write(repo / "drift.txt", "candidate drift\n")
    commit(repo, "move published candidate ref")
    git(repo, "push", "origin", f"HEAD:refs/heads/candidate/{SLUG}")
    (world.child_bares[repositories[1]] / "hooks" / "pre-receive").unlink()

    result = world.coordinate(
        world.author, "complete", "--slug", SLUG, check=False,
    )

    assert result.returncode == 1
    assert "candidate ref changed" in result.stderr
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()


def test_completion_rejects_candidate_drift_during_later_repository_push(
    world: World,
) -> None:
    repositories = tuple(world.child_bares)
    prepare_repositories(world, *repositories)
    validate_task_plan(world)
    first, second = repositories
    repo = world.author / first
    git(repo, "switch", f"candidate/{SLUG}")
    write(repo / "drift.txt", "candidate drift\n")
    drift = commit(repo, "prepare candidate drift")
    git(repo, "push", "origin", f"{drift}:refs/heads/drift")
    candidate_ref = f"refs/heads/candidate/{SLUG}"
    move_candidate_after_other_target_push(
        world.child_bares[second],
        world.child_bares[first],
        candidate_ref,
        drift,
    )

    result = world.coordinate(
        world.author, "complete", "--slug", SLUG, check=False,
    )

    assert result.returncode == 1
    assert "candidate changed" in result.stderr
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()


def test_completion_routes_conflict_after_partial_publication(world: World) -> None:
    repositories = tuple(world.child_bares)
    prepare_repositories(world, *repositories)
    validate_task_plan(world)
    second = repositories[1]
    repo = world.author / second
    git(repo, "switch", "main")
    write(repo / "value.txt", "conflicting target\n")
    concurrent = commit(repo, "advance second target")
    git(repo, "push", "origin", f"{concurrent}:refs/heads/concurrent")
    move_remote_ref_after_push_once(
        world.child_bares[second], "refs/heads/main", concurrent,
    )

    result = world.coordinate(world.author, "complete", "--slug", SLUG)

    assert result.stdout.startswith(f"CONFLICT\t{SLUG}\tAuthoring\t")
    first = repositories[0]
    assert not git(
        world.author / first,
        "merge-base",
        "--is-ancestor",
        json.loads(
            (world.author / "scratch" / SLUG / "candidate.json").read_text()
        )["repositories"][0]["candidate_sha"],
        remote_sha(world.author / first, "refs/heads/main"),
        check=False,
    ).returncode
    assert remote_sha(
        world.author / first, f"refs/heads/candidate/{SLUG}"
    ) is None
    assert remote_sha(
        world.author / first, f"refs/heads/validation/{SLUG}"
    ) is None
    assert remote_sha(
        world.author / second, f"refs/heads/candidate/{SLUG}"
    ) is not None
    assert remote_sha(
        world.author / second, f"refs/heads/validation/{SLUG}"
    ) is None
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()


def test_temporary_ref_cleanup_failure_does_not_gate_done(world: World) -> None:
    repository = "group/dependency"
    prepare_repositories(world, repository)
    validate_task_plan(world)
    candidate_ref = f"refs/heads/candidate/{SLUG}"
    reject_remote_ref(world.child_bares[repository], candidate_ref)

    result = world.coordinate(world.author, "complete", "--slug", SLUG)

    assert result.stdout.startswith("COMPLETED\t")
    assert SLUG in todo_section(world.author, "Done")
    assert remote_sha(world.author / repository, candidate_ref) is not None


def test_completion_infrastructure_failure_preserves_review_claim(world: World) -> None:
    repositories = tuple(world.child_bares)
    for repository in repositories:
        world.make_candidate(world.author, repository, f"candidate {repository}\n")
    claim(world, world.author)
    handoff(world, *repositories)
    validate_task_plan(world)
    git(
        world.author / repositories[1],
        "remote",
        "set-url",
        "origin",
        str(world.author.parent / "missing-remote.git"),
    )

    result = world.coordinate(world.author, "complete", "--slug", SLUG, check=False)

    assert result.returncode == 1
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").exists()


def test_create_ready_block_and_unblock_public_lifecycle(world: World) -> None:
    slug = "2026-07-03-public-lifecycle"
    created = world.coordinate(world.author, "create", slug)
    assert created.stdout == f"CREATED\t{slug}\tBacklog\n"
    assert (world.author / "scratch" / slug / "JOURNAL.md").read_text() == (
        "# Task Journal\n"
    )
    manifest = world.author / "scratch" / slug / "task.json"
    write(
        manifest,
        json.dumps({
            "dependencies": [],
            "capabilities": {},
            "resources": {},
            "repositories": ["group/dependency"],
        }),
    )

    ready = world.coordinate(world.author, "ready", slug)
    assert ready.stdout == f"READY\t{slug}\n"
    assert slug in todo_section(world.author, "Ready")

    write(world.author / "scratch" / slug / "blocker.md", "Waiting for an external decision.\n")
    blocked = world.coordinate(world.author, "block", slug)
    assert blocked.stdout == f"BLOCKED\t{slug}\n"
    assert slug in todo_section(world.author, "Blocked")

    write(world.author / "scratch" / slug / "resolution.md", "The decision is available.\n")
    unblocked = world.coordinate(world.author, "unblock", slug, "--to", "Backlog")
    assert unblocked.stdout == f"UNBLOCKED\t{slug}\tBacklog\n"
    assert (world.author / "scratch" / slug / "README.md").read_text() == (
        f"# {slug}\n"
    )
    journal = (world.author / "scratch" / slug / "JOURNAL.md").read_text()
    assert "## Entry 0001" in journal
    assert "**Resolution**" in journal
    assert "The decision is available." in journal
    assert "### Resolved blocker" in journal
    assert "Waiting for an external decision." in journal


def test_reviewer_creates_follow_up_without_routing_review(world: World) -> None:
    world.make_candidate(world.author, "group/dependency", "candidate\n")
    claim(world, world.author)
    handoff(world, "group/dependency")
    world.sync(world.author)
    claim(world, world.author)
    write(
        world.author / "scratch" / SLUG / "follow-up.md",
        "## Mission statement\n\nClean up the adjacent legacy path.\n",
    )
    follow_up = "2026-07-03-follow-up-legacy-path"

    result = world.coordinate(world.author, "follow-up", SLUG, follow_up)

    assert result.stdout == f"FOLLOW_UP\t{SLUG}\t{follow_up}\tBacklog\n"
    assert SLUG in todo_section(world.author, "Authoring")
    assert follow_up in todo_section(world.author, "Backlog")
    assert (world.author / "scratch" / SLUG / "claim.json").is_file()
    assert not (world.author / "scratch" / SLUG / "follow-up.md").exists()
    readme = (world.author / "scratch" / follow_up / "README.md").read_text()
    assert f"Follow-up from `{SLUG}`" in readme
    assert "Clean up the adjacent legacy path." in readme


def test_dependency_commands_preserve_order_and_clear_null(world: World) -> None:
    slug = "2026-07-03-dependency-command"
    world.coordinate(world.author, "create", slug)

    world.coordinate(world.author, "dependency", "add", slug, SLUG)
    data = json.loads((world.author / "scratch" / slug / "task.json").read_text())
    assert data["dependencies"] == [SLUG]

    world.coordinate(world.author, "dependency", "remove", slug, SLUG)
    world.coordinate(world.author, "dependency", "clear", slug)
    data = json.loads((world.author / "scratch" / slug / "task.json").read_text())
    assert data["dependencies"] == []


def test_dependency_workspace_option_accepts_both_positions() -> None:
    before = task.parser().parse_args(
        ["dependency", "--workspace", "/before", "add", "subject", "required"]
    )
    after = task.parser().parse_args(
        ["dependency", "add", "subject", "required", "--workspace", "/after"]
    )

    assert before.workspace == "/before"
    assert after.workspace == "/after"


def test_complete_failure_preserves_reviewing_claim(world: World) -> None:
    world.make_candidate(world.author, "group/dependency", "candidate\n")
    claim(world, world.author)
    handoff(world, "group/dependency")
    world.sync(world.author)
    claim(world, world.author)

    result = world.coordinate(
        world.author, "complete", "--slug", SLUG, check=False
    )

    assert result.returncode == 1
    assert "missing evidence" in result.stderr
    assert SLUG in todo_section(world.author, "Authoring")
    assert (world.author / "scratch" / SLUG / "claim.json").is_file()
    assert not (world.author / "scratch" / SLUG / "blocker.md").exists()


def test_ready_reconciles_unrelated_published_queue_change(world: World) -> None:
    slug = "2026-07-03-ready-reconcile"
    other = "2026-07-03-unrelated-create"
    world.coordinate(world.author, "create", slug)
    world.sync(world.author)
    write(
        world.author / "scratch" / slug / "task.json",
        json.dumps({"dependencies": [], "capabilities": {}, "resources": {}}),
    )
    commit(world.author, "resolve task requirements")
    git(world.author, "push", "origin", f"HEAD:{world.control_branch}")
    world.coordinate(world.author, "create", other)

    result = world.coordinate(world.author, "ready", slug)

    assert result.stdout == f"READY\t{slug}\n"
    assert slug in todo_section(world.author, "Ready")
    assert other in todo_section(world.author, "Backlog")
