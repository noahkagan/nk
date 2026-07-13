#!/usr/bin/env python3
"""Manage workspace task readiness, claims, review, and completion."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, Iterable


QUEUE_ORDER = (
    "Blocked", "Authoring", "Ready", "Done", "Backlog", "Cancelled",
)
BUCKETS = set(QUEUE_ORDER)
TASK_RE = re.compile(
    r"^- \[`([^`]+)`\]\((scratch/[^)]+/README\.md)\)$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LEGACY_CLAIM_FIELDS = {"owner", "claim_id"}
CLAIM_FIELDS = {*LEGACY_CLAIM_FIELDS, "spec_sha", "repositories"}
DEFERRED_CLAIM_FIELDS = {*CLAIM_FIELDS, "resume_after"}
CANDIDATE_FIELDS = {"slug", "author_owner", "repositories"}
CLAIMED_CANDIDATE_FIELDS = {*CANDIDATE_FIELDS, "spec_sha", "allowed_repositories"}
EVIDENCE_NAMES = (
    "candidate.json", "merge.json", "validation.json",
)
MANIFEST_FIELDS = {"dependencies", "capabilities", "resources"}
CAPABILITY_FIELDS = {"os", "architecture"}
RESOURCE_FIELDS = {"gpu"}
RUNTIME_PREFIX = ".workspace"
SLUG_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-"
    r"(?:(?:gh|lin)-\d+-|(?:[A-Z][A-Z0-9]+-\d+)-)?"
    r"[a-z0-9]+(?:-[a-z0-9]+)*$"
)
JOURNAL_ENTRY_RE = re.compile(r"^## Entry ([0-9]{4,})$")
MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
MARKDOWN_H1_H2_RE = re.compile(r"^ {0,3}#{1,2}(?:[ \t]+|$)")
MARKDOWN_SETEXT_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
CHECKPOINT_ID_RE = re.compile(
    r"^<!-- nk-checkpoint-id: ([0-9a-f]{32}) ([0-9a-f]{64}) -->$"
)
CHECKPOINT_PROTECTED_PATTERNS = (
    ("TODO.md", "queue placement", "nk task lifecycle commands"),
    ("scratch/<slug>/README.md", "live task specification", "the operator"),
    ("scratch/<slug>/task.json", "task placement manifest", "the operator"),
    ("scratch/<slug>/JOURNAL.md", "task Journal", "nk task lifecycle commands"),
    ("scratch/<slug>/claim.json", "task claim", "nk task claim"),
    ("scratch/<slug>/candidate.json", "candidate binding", "nk task submit"),
    ("scratch/<slug>/validation.json", "validation evidence", "nk task record-validation"),
    ("scratch/<slug>/merge.json", "merge evidence", "nk task complete"),
    ("scratch/<slug>/progress.md", "Checkpoint input", "nk task checkpoint"),
    ("scratch/<slug>/blocker.md", "Blocked input", "nk task block"),
    ("scratch/<slug>/cancellation.md", "cancellation input", "nk task cancel"),
    ("scratch/<slug>/resolution.md", "resolution input", "nk task unblock"),
    ("scratch/<slug>/follow-up.md", "follow-up input", "nk task follow-up"),
)


class CoordinationError(RuntimeError):
    pass


class RemoteAccessError(CoordinationError):
    pass


class PublicationError(CoordinationError):
    pass


class TargetMoved(CoordinationError):
    pass


class MergeConflict(CoordinationError):
    pass


class CandidatePreparationError(CoordinationError):
    pass


@dataclass(frozen=True)
class ControlBranch:
    name: str
    ref: str


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CoordinationError(f"git {' '.join(args)} failed: {detail}")
    return result


def remote_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return git(repo, *args)
    except CoordinationError as exc:
        raise RemoteAccessError(str(exc)) from exc


def owner(workspace: Path) -> str:
    marker = workspace / ".workspace/nk/ownership.json"
    try:
        identity = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        identity = None
    except json.JSONDecodeError as exc:
        raise CoordinationError(f"invalid workspace ownership marker: {marker}") from exc
    if identity is not None:
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("workspace"), str)
            or not identity["workspace"]
            or not isinstance(identity.get("node"), str)
            or not identity["node"]
        ):
            raise CoordinationError(f"invalid workspace ownership marker: {marker}")
        return f"{identity['workspace']}@{identity['node']}"
    value = os.environ.get("NK_WORKSPACE_OWNER")
    if value:
        return value
    return f"{workspace.name}@localhost"


def status_lines(workspace: Path, *, ignore_runtime: bool = True) -> list[str]:
    status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all").stdout
    return sorted(
        line for line in status.splitlines()
        if not ignore_runtime
        or not line[3:].split(" -> ", 1)[-1].startswith(RUNTIME_PREFIX)
    )


def ensure_clean(workspace: Path) -> None:
    lines = status_lines(workspace)
    if lines:
        raise CoordinationError("queue checkout is dirty:\n" + "\n".join(lines))


def ensure_tracked_clean(workspace: Path) -> None:
    status = git(workspace, "status", "--porcelain=v1", "--untracked-files=no").stdout
    if status:
        raise CoordinationError("queue checkout has tracked changes:\n" + status)


def ensure_published(workspace: Path) -> None:
    ensure_clean(workspace)
    local = git(workspace, "rev-parse", "HEAD").stdout.strip()
    upstream = git(workspace, "rev-parse", "@{upstream}", check=False)
    if upstream.returncode or local != upstream.stdout.strip():
        raise CoordinationError("queue checkout is not synchronized with published state")


def current_branch(workspace: Path) -> str:
    return git(workspace, "branch", "--show-current").stdout.strip()


def remote_default_branch(repo: Path) -> tuple[ControlBranch, str]:
    result = remote_git(repo, "ls-remote", "--symref", "origin", "HEAD")
    control = None
    head = None
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == "ref:" and fields[2] == "HEAD":
            ref = fields[1]
            prefix = "refs/heads/"
            if ref.startswith(prefix) and len(ref) > len(prefix):
                control = ControlBranch(ref[len(prefix):], ref)
        elif len(fields) == 2 and fields[1] == "HEAD" and SHA_RE.fullmatch(fields[0]):
            head = fields[0]
    if control is None or head is None:
        raise CoordinationError("origin does not advertise a default branch")
    return control, head


def resolve_default_branch(repo: Path) -> ControlBranch:
    return remote_default_branch(repo)[0]


def tracking_ref(ref: str) -> str:
    prefix = "refs/heads/"
    if not ref.startswith(prefix):
        raise CoordinationError(f"unsupported remote ref: {ref}")
    return f"refs/remotes/origin/{ref[len(prefix):]}"


def fetch_ref(repo: Path, ref: str) -> str:
    local = tracking_ref(ref)
    remote_git(repo, "fetch", "origin", f"+{ref}:{local}")
    value = git(repo, "rev-parse", local).stdout.strip()
    if SHA_RE.fullmatch(value) is None:
        raise CoordinationError(f"remote ref did not resolve to a commit: {ref}")
    return value


def remote_sha(repo: Path, ref: str) -> str | None:
    result = remote_git(repo, "ls-remote", "--heads", "origin", ref)
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref:
        raise CoordinationError(f"remote ref is ambiguous: {ref}")
    value = rows[0][0]
    if SHA_RE.fullmatch(value) is None:
        raise CoordinationError(f"remote ref is not a commit: {ref}")
    return value


def fetch_remote(repo: Path) -> tuple[ControlBranch, str]:
    remote_git(
        repo, "fetch", "--prune", "origin", "HEAD",
        "+refs/heads/*:refs/remotes/origin/*",
    )
    head = git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
    if SHA_RE.fullmatch(head) is None:
        raise CoordinationError("origin HEAD did not resolve to a commit")
    refs = git(
        repo, "for-each-ref", "--format=%(refname)", "--points-at", head,
        "refs/remotes/origin",
    ).stdout.splitlines()
    prefix = "refs/remotes/origin/"
    branches = [
        ref[len(prefix):]
        for ref in refs
        if ref.startswith(prefix) and ref != "refs/remotes/origin/HEAD"
    ]
    if len(branches) == 1:
        name = branches[0]
    else:
        advertised = resolve_default_branch(repo)
        if advertised.name not in branches:
            raise CoordinationError("origin default branch does not match HEAD")
        name = advertised.name
    return ControlBranch(name, f"refs/heads/{name}"), head


def fetched_sha(repo: Path, ref: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", tracking_ref(ref), check=False)
    if result.returncode:
        return None
    value = result.stdout.strip()
    if SHA_RE.fullmatch(value) is None:
        raise CoordinationError(f"remote ref is not a commit: {ref}")
    return value


def _acquire_file_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                    raise
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_file_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def coordination_lock(workspace: Path):
    lock_path = workspace / ".workspace" / "task-coordination.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b" ")
            handle.flush()
        handle.seek(0)
        _acquire_file_lock(handle)
        try:
            yield
        finally:
            handle.seek(0)
            _release_file_lock(handle)


@contextmanager
def mutation_guard(workspace: Path):
    with coordination_lock(workspace):
        try:
            ensure_clean(workspace)
            control = resolve_default_branch(workspace)
            if current_branch(workspace) != control.name:
                raise CoordinationError(
                    f"queue checkout must be on control branch {control.name}"
                )
            remote = fetch_ref(workspace, control.ref)
            local = git(workspace, "rev-parse", "HEAD").stdout.strip()
            if local != remote:
                raise CoordinationError(
                    f"queue checkout is not synchronized with origin/{control.name}"
                )
            ensure_clean(workspace)
        except CoordinationError as exc:
            raise PublicationError(str(exc)) from exc
        yield control, remote


def parse_todo(
    text: str, workspace: Path | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    bucket = None
    buckets: dict[str, str] = {}
    readmes: dict[str, str] = {}
    headings: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            bucket = line[3:]
            headings.append(bucket)
            continue
        match = TASK_RE.match(line)
        if not match:
            if bucket in BUCKETS and line.startswith("- "):
                raise CoordinationError(f"malformed TODO task entry: {line}")
            continue
        if bucket not in BUCKETS:
            raise CoordinationError(f"task entry is outside a queue: {line}")
        slug, readme = match.groups()
        if slug in buckets:
            raise CoordinationError(f"duplicate TODO task: {slug}")
        expected = f"scratch/{slug}/README.md"
        if readme != expected:
            raise CoordinationError(f"task link does not match slug: {slug}")
        if workspace is not None and not (workspace / readme).is_file():
            raise CoordinationError(f"task README is missing: {readme}")
        buckets[slug] = bucket
        readmes[slug] = readme
    if headings != list(QUEUE_ORDER):
        raise CoordinationError(
            "TODO queues must appear exactly once in this order: "
            + ", ".join(QUEUE_ORDER)
        )
    return buckets, readmes


def status(workspace: Path, slug: str) -> None:
    buckets, _ = parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"))
    bucket = buckets.get(slug)
    if bucket is None:
        raise CoordinationError(f"task is missing from TODO: {slug}")
    print(f"STATUS\t{slug}\t{bucket}")


def validate_new_slug(slug: str) -> None:
    match = SLUG_RE.fullmatch(slug)
    if match is None or any(value in slug for value in ("/", "\\", "..")):
        raise CoordinationError(f"invalid task slug: {slug}")
    try:
        datetime.strptime(match.group("date"), "%Y-%m-%d")
    except ValueError as exc:
        raise CoordinationError(f"invalid task slug date: {slug}") from exc


def insert_task(workspace: Path, slug: str, target: str) -> None:
    path = workspace / "TODO.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    heading_index = lines.index(f"## {target}")
    index = heading_index + 1
    while index < len(lines) and not lines[index].startswith("## "):
        index += 1
    while index > heading_index + 1 and lines[index - 1] == "":
        index -= 1
    lines.insert(index, f"- [`{slug}`](scratch/{slug}/README.md)")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def claim_paths_from_tree(workspace: Path, tree: str) -> list[str]:
    result = git(workspace, "ls-tree", "-r", "--name-only", tree, "--", "scratch")
    return sorted(path for path in result.stdout.splitlines() if path.endswith("/claim.json"))


def text_from_tree(workspace: Path, tree: str, path: str) -> str:
    return git(workspace, "show", f"{tree}:{path}").stdout


def validate_claim(data: object, path: str, buckets: dict[str, str]) -> dict[str, Any]:
    fields = frozenset(data) if isinstance(data, dict) else frozenset()
    if not isinstance(data, dict) or fields not in {
        frozenset(LEGACY_CLAIM_FIELDS), frozenset(CLAIM_FIELDS),
        frozenset(DEFERRED_CLAIM_FIELDS),
    }:
        raise CoordinationError(f"invalid claim fields: {path}")
    if not all(isinstance(data[field], str) and data[field] for field in LEGACY_CLAIM_FIELDS):
        raise CoordinationError(f"invalid claim values: {path}")
    if fields in {frozenset(CLAIM_FIELDS), frozenset(DEFERRED_CLAIM_FIELDS)} and (
        SHA_RE.fullmatch(data["spec_sha"]) is None
        or not valid_repositories(data["repositories"], allow_empty=True)
    ):
        raise CoordinationError(f"invalid claim values: {path}")
    if fields == DEFERRED_CLAIM_FIELDS:
        parse_resume_after(data["resume_after"])
    claim = dict(data)
    expected_slug = Path(path).parent.name
    bucket = buckets.get(expected_slug)
    if bucket != "Authoring":
        raise CoordinationError(f"claim does not match an activity queue: {path}")
    return {**claim, "slug": expected_slug}


def claims_from_tree(
    workspace: Path, tree: str, buckets: dict[str, str], strict: bool = True
) -> list[dict[str, Any]]:
    claims = []
    for path in claim_paths_from_tree(workspace, tree):
        try:
            data = json.loads(text_from_tree(workspace, tree, path))
        except json.JSONDecodeError as exc:
            if strict:
                raise CoordinationError(f"invalid JSON claim: {path}: {exc}") from exc
            continue
        try:
            claims.append(validate_claim(data, path, buckets))
        except CoordinationError:
            if strict:
                raise
    return claims


def observed_control_tree(workspace: Path) -> tuple[ControlBranch, str]:
    control, tree = remote_default_branch(workspace)
    if git(workspace, "cat-file", "-e", f"{tree}^{{commit}}", check=False).returncode:
        with coordination_lock(workspace):
            if git(
                workspace, "cat-file", "-e", f"{tree}^{{commit}}", check=False
            ).returncode:
                fetch_ref(workspace, control.ref)
                if git(
                    workspace, "cat-file", "-e", f"{tree}^{{commit}}", check=False
                ).returncode:
                    raise RemoteAccessError("observed remote HEAD could not be fetched")
    return control, tree


def claim_snapshot(workspace: Path) -> list[dict[str, str]]:
    _, tree = observed_control_tree(workspace)
    buckets, _ = parse_todo(text_from_tree(workspace, tree, "TODO.md"))
    return claims_from_tree(workspace, tree, buckets)


def queue_overview_from_tree(workspace: Path) -> dict[str, list[str]]:
    _, tree = observed_control_tree(workspace)
    buckets, _ = parse_todo(text_from_tree(workspace, tree, "TODO.md"))
    overview = {queue: [] for queue in QUEUE_ORDER}
    for slug, queue in buckets.items():
        overview[queue].append(slug)
    return overview


def local_state(workspace: Path) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    todo = (workspace / "TODO.md").read_text(encoding="utf-8")
    buckets, readmes = parse_todo(todo)
    claims = []
    for path in sorted((workspace / "scratch").glob("*/claim.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CoordinationError(f"invalid JSON claim: {path}: {exc}") from exc
        claims.append(validate_claim(data, relative_git_path(path, workspace), buckets))
    return buckets, readmes, claims


def owned_claims(claims: list[dict[str, Any]], workspace_owner: str) -> list[dict[str, Any]]:
    return [claim for claim in claims if claim["owner"] == workspace_owner]


def task_claim(
    workspace: Path, slug: str
) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    buckets, readmes, claims = local_state(workspace)
    matches = [claim for claim in claims if claim["slug"] == slug]
    if len(matches) != 1:
        raise CoordinationError(f"task does not have exactly one claim: {slug}")
    claim = matches[0]
    if claim["owner"] != owner(workspace):
        raise CoordinationError("claim belongs to another workspace owner")
    return buckets, readmes, claim


def move_todo(workspace: Path, slug: str, source: str, target: str) -> None:
    path = workspace / "TODO.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    task_line = None
    source_index = None
    for index, line in enumerate(lines):
        match = TASK_RE.match(line)
        if match and match.group(1) == slug:
            task_line, source_index = line, index
            break
    if task_line is None or source_index is None:
        raise CoordinationError(f"task is missing from TODO: {slug}")
    current = None
    for line in lines[:source_index + 1]:
        if line.startswith("## "):
            current = line[3:]
    if current != source:
        raise CoordinationError(f"task {slug} is in {current}, expected {source}")
    if source == target:
        return
    del lines[source_index]
    heading = f"## {target}"
    try:
        heading_index = lines.index(heading)
    except ValueError as exc:
        raise CoordinationError(f"missing TODO bucket: {target}") from exc
    target_index = heading_index + 1
    while target_index < len(lines) and not lines[target_index].startswith("## "):
        target_index += 1
    while target_index > heading_index + 1 and lines[target_index - 1] == "":
        target_index -= 1
    lines.insert(target_index, task_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def changed_paths(workspace: Path) -> set[str]:
    result = git(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = result.stdout.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        if not entry:
            index += 1
            continue
        status = entry[:2]
        path = entry[3:]
        if not path.startswith(RUNTIME_PREFIX):
            paths.add(path)
        index += 2 if status[0] in {"R", "C"} else 1
    return paths


def commit_and_push(
    workspace: Path,
    control: ControlBranch,
    expected_sha: str,
    message: str,
    relative_paths: list[str],
) -> str:
    allowed = set(relative_paths)
    actual = changed_paths(workspace)
    if not actual or not actual.issubset(allowed):
        raise CoordinationError(
            f"unexpected coordination changes: {sorted(actual - allowed)}"
        )
    stage_paths = [
        path for path in relative_paths
        if (workspace / path).exists()
        or git(workspace, "ls-files", "--error-unmatch", "--", path, check=False).returncode == 0
    ]
    if not stage_paths:
        raise CoordinationError("coordination operation produced no stageable changes")
    git(workspace, "add", "--all", "--", *stage_paths)
    git(workspace, "commit", "-m", message)
    generated = git(workspace, "rev-parse", "HEAD").stdout.strip()
    pushed = push_control_ref(workspace, control, expected_sha)
    if pushed.returncode != 0:
        raise PublicationError(
            f"coordination push failed or is uncertain; inspect remote and local commit {generated} manually"
        )
    remote = fetch_ref(workspace, control.ref)
    if git(workspace, "merge-base", "--is-ancestor", generated, remote, check=False).returncode:
        raise PublicationError("generated coordination commit is not published")
    return generated


def push_control_ref(
    repo: Path, control: ControlBranch, expected_sha: str, source: str = "HEAD"
) -> subprocess.CompletedProcess[str]:
    return git(
        repo,
        "push",
        f"--force-with-lease={control.ref}:{expected_sha}",
        "origin",
        f"{source}:{control.ref}",
        check=False,
    )


def canonical_bytes(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data: object) -> str:
    return hashlib.sha256(canonical_bytes(data)).hexdigest()


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CoordinationError(f"missing evidence: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise CoordinationError(f"invalid JSON evidence: {path.name}: {exc}") from exc


def task_dir(workspace: Path, slug: str) -> Path:
    path = workspace / "scratch" / slug
    if not path.is_dir():
        raise CoordinationError(f"task scratch directory is missing: {slug}")
    return path


def relative_git_path(path: PurePath, workspace: PurePath) -> str:
    return path.relative_to(workspace).as_posix()


def normalized_repository(workspace: Path, value: str) -> tuple[str, Path]:
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise CoordinationError(f"repository path must be workspace-relative: {value}")
    normalized = pure.as_posix()
    path = (workspace / normalized).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise CoordinationError(f"repository escapes workspace: {value}") from exc
    if not path.exists():
        raise CoordinationError(f"repository is missing: {value}")
    if git(path, "rev-parse", "--is-inside-work-tree", check=False).returncode:
        raise CoordinationError(f"repository is not a Git worktree: {value}")
    return normalized, path


def ensure_claim_repositories_clean(
    workspace: Path, claim: dict[str, Any]
) -> None:
    dirty = []
    for value in sorted(claim["repositories"]):
        relative, repo = normalized_repository(workspace, value)
        lines = status_lines(repo, ignore_runtime=False)
        if lines:
            dirty.append(f"{relative}:\n" + "\n".join(f"  {line}" for line in lines))
    if dirty:
        raise CoordinationError(
            "claimed candidate repositories are dirty:\n" + "\n".join(dirty)
        )


def ensure_claim_repositories_published(
    workspace: Path, claim: dict[str, Any]
) -> None:
    ensure_claim_repositories_clean(workspace, claim)
    for value in sorted(claim["repositories"]):
        relative, repo = normalized_repository(workspace, value)
        child, remote = fetch_remote(repo)
        branch = current_branch(repo)
        local = git(repo, "rev-parse", "HEAD").stdout.strip()
        if branch == f"candidate/{claim['slug']}":
            published = fetched_sha(
                repo, f"refs/heads/candidate/{claim['slug']}"
            )
        elif branch == child.name:
            published = remote
        elif not branch:
            containing = git(
                repo, "branch", "-r", "--contains", local, check=False
            ).stdout.strip()
            if containing:
                continue
            published = None
        else:
            raise CoordinationError(
                f"claimed candidate repository is on an unrelated branch: "
                f"{relative}: {branch}"
            )
        if published is None or git(
            repo, "merge-base", "--is-ancestor", local, published, check=False
        ).returncode:
            raise CoordinationError(
                f"claimed candidate repository is not fully pushed: {relative}"
            )


def ensure_claim_release(workspace: Path, slug: str) -> dict[str, Any]:
    _, _, claim = task_claim(workspace, slug)
    ensure_claim_repositories_published(workspace, claim)
    return claim


def candidate_entry(workspace: Path, slug: str, value: str) -> dict[str, str]:
    relative, repo = normalized_repository(workspace, value)
    target = resolve_default_branch(repo)
    target_sha = fetch_ref(repo, target.ref)
    candidate_ref = f"refs/heads/candidate/{slug}"
    candidate_sha = remote_sha(repo, candidate_ref)
    if candidate_sha is None:
        raise CoordinationError(f"missing remote candidate ref: {relative}")
    git(repo, "fetch", "origin", f"+{candidate_ref}:{tracking_ref(candidate_ref)}")
    base = git(repo, "merge-base", target_sha, candidate_sha, check=False)
    if base.returncode or SHA_RE.fullmatch(base.stdout.strip()) is None:
        raise CoordinationError(f"candidate has no merge base with target: {relative}")
    base_sha = base.stdout.strip()
    if git(repo, "merge-base", "--is-ancestor", base_sha, candidate_sha, check=False).returncode:
        raise CoordinationError(f"candidate is not descended from its base: {relative}")
    return {
        "path": relative,
        "target_ref": target.ref,
        "base_sha": base_sha,
        "candidate_sha": candidate_sha,
    }


def validate_candidate(data: Any, slug: str) -> dict[str, Any]:
    fields = frozenset(data) if isinstance(data, dict) else frozenset()
    if not isinstance(data, dict) or fields not in {
        frozenset(CANDIDATE_FIELDS), frozenset(CLAIMED_CANDIDATE_FIELDS)
    }:
        raise CoordinationError("candidate manifest fields are invalid")
    if data.get("slug") != slug or not isinstance(data.get("author_owner"), str) or not data["author_owner"]:
        raise CoordinationError("candidate manifest identity is invalid")
    repositories = data.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise CoordinationError("candidate manifest has no repositories")
    seen = set()
    for entry in repositories:
        if not isinstance(entry, dict) or set(entry) != {
            "path", "target_ref", "base_sha", "candidate_sha"
        }:
            raise CoordinationError("candidate repository entry is invalid")
        if not all(isinstance(entry.get(key), str) and entry[key] for key in entry):
            raise CoordinationError("candidate repository values are invalid")
        if entry["path"] in seen or SHA_RE.fullmatch(entry["base_sha"]) is None or SHA_RE.fullmatch(entry["candidate_sha"]) is None:
            raise CoordinationError("candidate repository identity is invalid")
        if not entry["target_ref"].startswith("refs/heads/"):
            raise CoordinationError("candidate target ref is invalid")
        seen.add(entry["path"])
    if fields == CLAIMED_CANDIDATE_FIELDS and (
        SHA_RE.fullmatch(data["spec_sha"]) is None
        or not valid_repositories(data["allowed_repositories"])
    ):
        raise CoordinationError("candidate claim contract is invalid")
    return data


def load_candidate(workspace: Path, slug: str) -> dict[str, Any]:
    return validate_candidate(read_json(task_dir(workspace, slug) / "candidate.json"), slug)


def verify_candidate_refs(workspace: Path, slug: str, candidate: dict[str, Any]) -> None:
    for entry in candidate["repositories"]:
        _, repo = normalized_repository(workspace, entry["path"])
        ref = f"refs/heads/candidate/{slug}"
        if remote_sha(repo, ref) != entry["candidate_sha"]:
            raise CoordinationError(f"candidate ref changed: {entry['path']}")


def evidence_paths(workspace: Path, slug: str) -> dict[str, Path]:
    directory = task_dir(workspace, slug)
    return {name: directory / name for name in EVIDENCE_NAMES}


def remove_evidence(paths: dict[str, Path], names: Iterable[str]) -> None:
    for name in names:
        paths[name].unlink(missing_ok=True)


def optional_json_from_tree(workspace: Path, tree: str, path: str) -> Any | None:
    result = git(workspace, "show", f"{tree}:{path}", check=False)
    if result.returncode:
        missing = git(workspace, "cat-file", "-e", f"{tree}:{path}", check=False)
        if missing.returncode:
            return None
        raise CoordinationError(f"could not read {path} from control state")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CoordinationError(f"invalid JSON in {path}: {exc}") from exc


def valid_repositories(value: object, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) for item in value)
        and len(value) == len(set(value))
        and all(
            item
            and not PurePosixPath(item).is_absolute()
            and all(part not in {"", ".", ".."} for part in PurePosixPath(item).parts)
            for item in value
        )
    )


def validate_manifest(data: Any, slug: str, *, require_ready: bool = False) -> dict[str, Any]:
    fields = frozenset(data) if isinstance(data, dict) else frozenset()
    if not isinstance(data, dict) or fields not in {
        frozenset(MANIFEST_FIELDS), frozenset({*MANIFEST_FIELDS, "repositories"})
    }:
        raise CoordinationError(f"task manifest fields are invalid: {slug}")
    dependencies = data["dependencies"]
    capabilities = data["capabilities"]
    resources = data["resources"]
    repositories = data.get("repositories")
    if dependencies is not None and (
        not isinstance(dependencies, list)
        or any(not isinstance(value, str) or not value for value in dependencies)
        or len(dependencies) != len(set(dependencies))
    ):
        raise CoordinationError(f"task dependencies are invalid: {slug}")
    if capabilities is not None and (
        not isinstance(capabilities, dict)
        or not set(capabilities).issubset(CAPABILITY_FIELDS)
        or any(not isinstance(value, str) or not value for value in capabilities.values())
    ):
        raise CoordinationError(f"task capabilities are invalid: {slug}")
    if resources is not None and (
        not isinstance(resources, dict)
        or not set(resources).issubset(RESOURCE_FIELDS)
        or any(not isinstance(value, int) or value < 0 for value in resources.values())
    ):
        raise CoordinationError(f"task resources are invalid: {slug}")
    if "repositories" in data and repositories is not None and not valid_repositories(repositories):
        raise CoordinationError(f"task repositories are invalid: {slug}")
    if require_ready and any(value is None for value in data.values()):
        raise CoordinationError(f"task manifest is unresolved: {slug}")
    return data


def manifest_from_tree(
    workspace: Path, tree: str, slug: str, *, require_ready: bool = False
) -> dict[str, Any]:
    path = f"scratch/{slug}/task.json"
    data = optional_json_from_tree(workspace, tree, path)
    if data is None:
        raise CoordinationError(f"task manifest is missing: {slug}")
    return validate_manifest(data, slug, require_ready=require_ready)


def manifest_from_checkout(
    workspace: Path, slug: str, *, require_ready: bool = False
) -> dict[str, Any]:
    path = workspace / "scratch" / slug / "task.json"
    if not path.is_file():
        raise CoordinationError(f"task manifest is missing: {slug}")
    return validate_manifest(read_json(path), slug, require_ready=require_ready)


def validate_dependencies(
    dependencies: dict[str, list[str]], buckets: dict[str, str]
) -> dict[str, list[str]]:
    for slug, values in dependencies.items():
        for dependency in values:
            if dependency == slug:
                raise CoordinationError(f"task depends on itself: {slug}")
            if dependency not in buckets:
                raise CoordinationError(f"task dependency is missing: {dependency}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(slug: str) -> None:
        if slug in visiting:
            raise CoordinationError("task dependencies contain a cycle")
        if slug in visited:
            return
        visiting.add(slug)
        for dependency in dependencies.get(slug, []):
            if dependency in dependencies:
                visit(dependency)
        visiting.remove(slug)
        visited.add(slug)

    for slug in dependencies:
        visit(slug)
    return dependencies


def dependencies_from_tree(
    workspace: Path, tree: str, buckets: dict[str, str]
) -> dict[str, list[str]]:
    dependencies: dict[str, list[str]] = {}
    for slug in buckets:
        data = optional_json_from_tree(workspace, tree, f"scratch/{slug}/task.json")
        if data is None:
            continue
        manifest = validate_manifest(data, slug)
        if manifest["dependencies"] is not None:
            dependencies[slug] = manifest["dependencies"]
    return validate_dependencies(dependencies, buckets)


def dependencies_from_checkout(
    workspace: Path, buckets: dict[str, str]
) -> dict[str, list[str]]:
    dependencies: dict[str, list[str]] = {}
    for slug in buckets:
        path = workspace / "scratch" / slug / "task.json"
        if not path.is_file():
            continue
        manifest = validate_manifest(read_json(path), slug)
        if manifest["dependencies"] is not None:
            dependencies[slug] = manifest["dependencies"]
    return validate_dependencies(dependencies, buckets)


def candidate_from_tree(workspace: Path, tree: str, slug: str) -> dict[str, Any]:
    path = f"scratch/{slug}/candidate.json"
    data = optional_json_from_tree(workspace, tree, path)
    if data is None:
        raise CoordinationError(f"missing evidence: {path}")
    return validate_candidate(data, slug)


def select_task(
    workspace: Path,
    tree: str,
    requested_slug: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    buckets, _ = parse_todo(text_from_tree(workspace, tree, "TODO.md"))
    claims = claims_from_tree(workspace, tree, buckets, strict=False)
    workspace_owner = owner(workspace)
    owned = owned_claims(claims, workspace_owner)
    if len(owned) > 1:
        raise CoordinationError("workspace owns multiple claims; reconcile them first")
    if owned:
        current = owned[0]
        if requested_slug is not None and requested_slug != current["slug"]:
            raise CoordinationError(
                f"workspace owns {current['slug']}; cannot claim {requested_slug}"
            )
        return current["slug"], current

    by_slug = {claim["slug"]: claim for claim in claims}
    source = "Ready"
    if requested_slug is not None and buckets.get(requested_slug) != source:
        raise CoordinationError(f"task {requested_slug} is not in {source}")
    if requested_slug is None:
        dependencies = dependencies_from_tree(workspace, tree, buckets)
    else:
        manifest = manifest_from_tree(
            workspace, tree, requested_slug, require_ready=True
        )
        dependencies = validate_dependencies(
            {requested_slug: manifest["dependencies"]}, buckets
        )
    candidates = [
        slug for slug, bucket in buckets.items()
        if bucket == source and (requested_slug is None or slug == requested_slug)
    ]

    for slug in candidates:
        if requested_slug is None:
            manifest_from_tree(workspace, tree, slug, require_ready=True)
        existing = by_slug.get(slug)
        if existing is not None:
            raise CoordinationError(f"task already has a claim: {slug}")
        unresolved = [
            dependency
            for dependency in dependencies.get(slug, [])
            if buckets.get(dependency) != "Done"
        ]
        if unresolved:
            if requested_slug is not None:
                raise CoordinationError(
                    f"task {slug} has unresolved dependencies: {','.join(unresolved)}"
                )
            continue
        return slug, None
    if requested_slug is not None:
        raise CoordinationError(f"task {requested_slug} cannot be claimed")
    return None, None


def synchronize_checkout(repo: Path, control: ControlBranch, remote: str) -> None:
    ensure_clean(repo)
    if current_branch(repo) != control.name:
        raise CoordinationError(f"queue checkout must be on control branch {control.name}")
    local = git(repo, "rev-parse", "HEAD").stdout.strip()
    if git(repo, "merge-base", "--is-ancestor", local, remote, check=False).returncode:
        raise CoordinationError(f"queue checkout diverged from origin/{control.name}")
    if local != remote:
        git(repo, "merge", "--ff-only", remote)
    ensure_clean(repo)


def prepare_children(
    workspace: Path,
    candidate: dict[str, Any] | None = None,
    claim: dict[str, Any] | None = None,
) -> None:
    meta_path = workspace / ".meta"
    projects: dict[str, str | None] = {}
    if meta_path.exists():
        data = read_json(meta_path)
        if (
            not isinstance(data, dict)
            or set(data) != {"projects"}
            or not isinstance(data["projects"], dict)
        ):
            raise CoordinationError(".meta must contain a projects object")
        for relative, url in data["projects"].items():
            if (
                not isinstance(relative, str)
                or not relative
                or not isinstance(url, str)
                or not url
            ):
                raise CoordinationError(".meta contains an invalid project")
            projects[relative] = url
    candidate_entries = (
        {entry["path"]: entry for entry in candidate["repositories"]}
        if candidate is not None
        else {}
    )
    for relative in candidate_entries:
        projects.setdefault(relative, None)
    repositories: list[tuple[Path, str, str]] = []
    empty_repositories: list[Path] = []
    for relative, url in projects.items():
        if not isinstance(relative, str) or not relative:
            raise CoordinationError(".meta contains an invalid project")
        if url is None and relative not in candidate_entries:
            raise CoordinationError(".meta contains an invalid project")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise CoordinationError(f"invalid .meta project path: {relative}")
        path = workspace / pure
        if not path.exists():
            if url is None:
                raise CandidatePreparationError(
                    f"candidate repository is missing: {relative}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            remote_git(workspace, "clone", url, str(path))
        _, repo = normalized_repository(workspace, relative)
        ensure_tracked_clean(repo)
        branch = current_branch(repo)
        if (
            git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode
            and not remote_git(repo, "ls-remote", "--heads", "origin").stdout.strip()
        ):
            empty_repositories.append(repo)
            continue
        repositories.append((repo, relative, branch))

    if not repositories:
        for repo in empty_repositories:
            git(repo, "clean", "-fdx")
        return
    with ThreadPoolExecutor(max_workers=len(repositories)) as executor:
        fetched = list(executor.map(
            fetch_remote, (repo for repo, _, _ in repositories)
        ))

    claimed_repositories = set(claim["repositories"]) if claim is not None else set()
    claimed_branch = f"candidate/{claim['slug']}" if claim is not None else None
    prepared: list[tuple[Path, str, ControlBranch, str, str, bool, bool]] = []
    for (repo, relative, branch), (child, remote) in zip(
        repositories, fetched
    ):
        retain_claimed_candidate = (
            relative in claimed_repositories and branch == claimed_branch
        )
        if (
            relative in claimed_repositories
            and branch not in {child.name, claimed_branch}
        ):
            raise CoordinationError(
                f"claimed candidate repository is on an unrelated branch: "
                f"{relative}: {branch or 'detached'}"
            )
        if not branch:
            local = git(repo, "rev-parse", "HEAD").stdout.strip()
            candidate_entry_data = candidate_entries.get(relative)
            exact_candidate = (
                candidate_entry_data is not None
                and local == candidate_entry_data["candidate_sha"]
            )
            if not exact_candidate:
                if candidate is None:
                    raise CoordinationError(
                        f"child checkout is detached: {relative}"
                    )
                reachable = git(
                    repo, "branch", "-r", "--contains", local, check=False
                ).stdout.strip()
                if not reachable:
                    raise CoordinationError(
                        f"child detached commit is not fully pushed: {relative}"
                    )
        elif branch != child.name and not retain_claimed_candidate:
            local = git(repo, "rev-parse", "HEAD").stdout.strip()
            published = fetched_sha(repo, f"refs/heads/{branch}")
            preserved = published is not None and not git(
                repo, "merge-base", "--is-ancestor", local, published, check=False,
            ).returncode
            if not preserved:
                preserved = not git(
                    repo, "merge-base", "--is-ancestor", local, remote, check=False,
                ).returncode
            if not preserved:
                raise CoordinationError(
                    f"child checkout is not fully pushed: {relative}: {branch}"
                )
        local_default = git(
            repo, "rev-parse", f"refs/heads/{child.name}", check=False
        )
        missing_default = local_default.returncode != 0
        if not missing_default and git(
            repo, "merge-base", "--is-ancestor",
            local_default.stdout.strip(), remote, check=False,
        ).returncode:
            raise CoordinationError(
                f"child checkout diverged from origin/{child.name}: {relative}"
            )
        candidate_entry_data = candidate_entries.get(relative)
        if candidate_entry_data is not None:
            candidate_ref = f"refs/heads/candidate/{candidate['slug']}"
            if (
                fetched_sha(repo, candidate_ref)
                != candidate_entry_data["candidate_sha"]
            ):
                raise CandidatePreparationError(f"candidate ref changed: {relative}")
        prepared.append((
            repo, relative, child, remote, branch, missing_default,
            retain_claimed_candidate,
        ))

    for repo in empty_repositories:
        git(repo, "clean", "-fdx")
    for (
        repo, relative, child, remote, branch, missing_default,
        retain_claimed_candidate,
    ) in prepared:
        if retain_claimed_candidate:
            ensure_clean(repo)
            continue
        git(repo, "clean", "-fdx")
        if branch != child.name:
            if missing_default:
                git(repo, "branch", child.name, remote)
            git(repo, "checkout", child.name)
        local = git(repo, "rev-parse", "HEAD").stdout.strip()
        if git(repo, "merge-base", "--is-ancestor", local, remote, check=False).returncode:
            raise CoordinationError(f"child checkout diverged from origin/{child.name}: {relative}")
        if local != remote:
            git(repo, "merge", "--ff-only", remote)
        candidate_entry_data = candidate_entries.get(relative)
        if candidate_entry_data is not None:
            git(repo, "checkout", "--detach", candidate_entry_data["candidate_sha"])
        ensure_clean(repo)


def publish_claim(
    workspace: Path,
    control: ControlBranch,
    tree: str,
    slug: str,
    claim_id: str,
) -> tuple[str, str] | None:
    with tempfile.TemporaryDirectory(prefix="task-claim-") as directory:
        temporary = Path(directory)
        git(workspace, "worktree", "add", "--detach", str(temporary), tree)
        try:
            source = "Ready"
            target = "Authoring"
            move_todo(temporary, slug, source, target)
            candidate_data = optional_json_from_tree(
                workspace, tree, f"scratch/{slug}/candidate.json"
            )
            candidate = (
                validate_candidate(candidate_data, slug)
                if candidate_data is not None else None
            )
            manifest = manifest_from_tree(workspace, tree, slug, require_ready=True)
            write_json(
                temporary / "scratch" / slug / "claim.json",
                {
                    "owner": owner(workspace),
                    "claim_id": claim_id,
                    "spec_sha": (
                        candidate.get("spec_sha", tree) if candidate else tree
                    ),
                    "repositories": (
                        candidate.get("allowed_repositories", [])
                        if candidate else manifest.get("repositories", [])
                    ),
                },
            )
            git(temporary, "add", "TODO.md", f"scratch/{slug}/claim.json")
            git(temporary, "commit", "-m", f"Claim task {slug}")
            generated = git(temporary, "rev-parse", "HEAD").stdout.strip()
            pushed = push_control_ref(temporary, control, tree)
            observed = fetch_ref(workspace, control.ref)
            buckets, _ = parse_todo(text_from_tree(workspace, observed, "TODO.md"))
            claims = claims_from_tree(workspace, observed, buckets, strict=False)
            landed = any(
                claim["claim_id"] == claim_id
                and claim["slug"] == slug
                and claim["owner"] == owner(workspace)
                for claim in claims
            )
            if landed:
                return generated, observed
            if pushed.returncode == 0:
                raise CoordinationError("published claim is missing from remote state")
            if observed == tree:
                detail = pushed.stderr.strip() or pushed.stdout.strip()
                raise CoordinationError(f"coordination push failed: {detail}")
            return None
        finally:
            git(workspace, "worktree", "remove", "--force", str(temporary))


def claim(
    workspace: Path, requested_slug: str | None, *, emit: bool = True
) -> dict[str, Any]:
    with coordination_lock(workspace):
        control = resolve_default_branch(workspace)
        while True:
            tree = fetch_ref(workspace, control.ref)
            slug, existing = select_task(workspace, tree, requested_slug)
            if existing is not None:
                ignore_local_path(workspace, f"scratch/{slug}/progress.md")
                synchronize_checkout(workspace, control, tree)
                prepare_children(workspace, claim=existing)
                outcome = {
                    "status": "resumed", "slug": slug,
                    "claim_id": existing["claim_id"],
                }
                if emit:
                    print(f"RESUMED\t{slug}\t{existing['claim_id']}")
                return outcome
            if slug is None:
                if emit:
                    print("EMPTY")
                return {
                    "status": "empty", "slug": None, "claim_id": None,
                }

            synchronize_checkout(workspace, control, tree)
            if existing is None:
                prepare_children(workspace)

            current = fetch_ref(workspace, control.ref)
            if current != tree:
                synchronize_checkout(workspace, control, current)
                continue
            selected, existing = select_task(
                workspace, current, requested_slug
            )
            if selected != slug:
                if requested_slug is not None:
                    raise CoordinationError(
                        f"task {requested_slug} changed before claim publication"
                    )
                synchronize_checkout(workspace, control, current)
                continue
            if existing is not None:
                ignore_local_path(workspace, f"scratch/{selected}/progress.md")
                synchronize_checkout(workspace, control, current)
                prepare_children(workspace, claim=existing)
                outcome = {
                    "status": "resumed", "slug": selected,
                    "claim_id": existing["claim_id"],
                }
                if emit:
                    print(f"RESUMED\t{selected}\t{existing['claim_id']}")
                return outcome
            claim_id = uuid.uuid4().hex
            publication = publish_claim(
                workspace, control, current, slug, claim_id
            )
            if publication is None:
                latest = fetch_ref(workspace, control.ref)
                synchronize_checkout(workspace, control, latest)
                continue
            _, observed = publication
            synchronize_checkout(workspace, control, observed)
            ignore_local_path(workspace, f"scratch/{slug}/progress.md")
            outcome = {
                "status": "claimed", "slug": slug, "claim_id": claim_id,
            }
            if emit:
                print(f"CLAIMED\t{slug}\t{claim_id}")
            return outcome


def submit(workspace: Path, slug: str, repositories: list[str]) -> None:
    if not repositories or len(repositories) != len(set(repositories)):
        raise CoordinationError("submit requires unique ordered repositories")
    with mutation_guard(workspace) as (control, expected_sha):
        buckets, _, current = task_claim(workspace, slug)
        if buckets.get(slug) != "Authoring":
            raise CoordinationError("submit requires an Authoring task")
        allowed = current.get("repositories", [])
        unexpected = sorted(set(repositories) - set(allowed)) if allowed else []
        if unexpected:
            raise CoordinationError(
                f"candidate adds repositories outside the claim: {unexpected}"
            )
        entries = [candidate_entry(workspace, slug, value) for value in repositories]
        manifest = {
            "slug": slug,
            "author_owner": current["owner"],
            "spec_sha": current.get("spec_sha", expected_sha),
            "allowed_repositories": allowed or repositories,
            "repositories": entries,
        }
        paths = evidence_paths(workspace, slug)
        changed = list(paths.values())
        write_json(paths["candidate.json"], manifest)
        remove_evidence(paths, EVIDENCE_NAMES[1:])
        relative = [relative_git_path(path, workspace) for path in changed]
        commit_and_push(workspace, control, expected_sha, f"Submit task {slug}", relative)
        print(f"SUBMITTED\t{slug}\tAuthoring")


def make_merge(repo: Path, target_sha: str, candidate_sha: str) -> str:
    with tempfile.TemporaryDirectory(prefix="task-merge-") as directory:
        worktree = Path(directory)
        git(repo, "worktree", "add", "--detach", str(worktree), target_sha)
        try:
            merged = git(worktree, "merge", "--no-ff", "--no-edit", candidate_sha, check=False)
            if merged.returncode:
                conflicts = git(
                    worktree, "diff", "--name-only", "--diff-filter=U",
                    check=False,
                ).stdout.splitlines()
                if conflicts:
                    raise MergeConflict(
                        f"candidate merge conflicts: {', '.join(conflicts)}"
                    )
                detail = merged.stderr.strip() or merged.stdout.strip()
                raise CoordinationError(f"candidate merge failed: {detail}")
            merge_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
            parents = git(worktree, "show", "-s", "--format=%P", merge_sha).stdout.split()
            if parents != [target_sha, candidate_sha]:
                raise CoordinationError("prepared merge does not have exact target and candidate parents")
            return merge_sha
        finally:
            git(repo, "worktree", "remove", "--force", str(worktree), check=False)


def _prepare(workspace: Path, slug: str) -> None:
    with mutation_guard(workspace) as (control, expected_sha):
        buckets, _, current = task_claim(workspace, slug)
        if buckets.get(slug) != "Authoring":
            raise CoordinationError("merge preparation requires an Authoring task")
        ensure_claim_release(workspace, slug)
        candidate = load_candidate(workspace, slug)
        validation = load_validation(workspace, slug, candidate)
        validation_binding_current(workspace, candidate, validation)
        paths = evidence_paths(workspace, slug)
        existing = (
            load_merge(workspace, slug, candidate)
            if paths["merge.json"].exists()
            else None
        )
        merge_ref = f"refs/heads/validation/{slug}"
        prepared: list[tuple[Path, dict[str, str], str, str, bool]] = []
        published_paths: list[str] = []
        for index, entry in enumerate(candidate["repositories"]):
            _, repo = normalized_repository(workspace, entry["path"])
            target_sha = fetch_ref(repo, entry["target_ref"])
            fetched_candidate = fetch_ref(
                repo, f"refs/heads/candidate/{slug}"
            )
            if fetched_candidate != entry["candidate_sha"]:
                raise CoordinationError(f"candidate ref changed: {entry['path']}")
            old = existing["repositories"][index] if existing is not None else None
            if old is not None and not git(
                repo, "merge-base", "--is-ancestor",
                entry["candidate_sha"], target_sha, check=False,
            ).returncode:
                prepared.append((repo, entry, old["target_sha"], old["merge_sha"], True))
                published_paths.append(entry["path"])
                continue
            if (
                old is not None
                and target_sha == old["target_sha"]
                and remote_sha(repo, merge_ref) == old["merge_sha"]
            ):
                merge_sha = old["merge_sha"]
            else:
                try:
                    merge_sha = make_merge(repo, target_sha, entry["candidate_sha"])
                except MergeConflict as exc:
                    published = (
                        f" Already published: {', '.join(published_paths)}."
                        if published_paths else ""
                    )
                    raise MergeConflict(f"{entry['path']}: {exc}.{published}") from exc
            prepared.append((repo, entry, target_sha, merge_sha, False))
        for repo, entry, target_sha, _, integrated in prepared:
            observed = fetch_ref(repo, entry["target_ref"])
            if integrated:
                if git(
                    repo, "merge-base", "--is-ancestor",
                    entry["candidate_sha"], observed, check=False,
                ).returncode:
                    raise TargetMoved(entry["path"])
                continue
            if observed != target_sha:
                raise TargetMoved(entry["path"])
            if remote_sha(repo, f"refs/heads/candidate/{slug}") != entry["candidate_sha"]:
                raise CoordinationError(
                    f"candidate changed during merge preparation: {entry['path']}"
                )
        published_refs: list[Path] = []
        for repo, entry, _, merge_sha, integrated in prepared:
            if integrated:
                continue
            pushed = git(repo, "push", "origin", f"+{merge_sha}:{merge_ref}", check=False)
            if pushed.returncode:
                cleanup_failed = False
                for published_repo in published_refs:
                    cleanup = git(
                        published_repo, "push", "origin", "--delete", merge_ref,
                        check=False,
                    )
                    cleanup_failed = cleanup_failed or cleanup.returncode != 0
                if cleanup_failed:
                    raise CoordinationError(
                        "prepared merge publication failed and temporary-ref cleanup requires manual intervention"
                    )
                raise CoordinationError(f"failed to publish prepared merge for {entry['path']}")
            published_refs.append(repo)
        data = {
            "slug": slug,
            "candidate_digest": digest(candidate),
            "repositories": [
                {
                    "path": entry["path"],
                    "target_ref": entry["target_ref"],
                    "target_sha": target_sha,
                    "candidate_sha": entry["candidate_sha"],
                    "merge_sha": merge_sha,
                }
                for repo, entry, target_sha, merge_sha, _ in prepared
            ],
        }
        if existing == data:
            return
        changed = [paths["merge.json"]]
        write_json(paths["merge.json"], data)
        relative = [relative_git_path(path, workspace) for path in changed]
        commit_and_push(workspace, control, expected_sha, f"Prepare merge for {slug}", relative)


def load_merge(workspace: Path, slug: str, candidate: dict[str, Any]) -> dict[str, Any]:
    data = read_json(evidence_paths(workspace, slug)["merge.json"])
    if not isinstance(data, dict) or set(data) != {"slug", "candidate_digest", "repositories"}:
        raise CoordinationError("merge evidence fields are invalid")
    if data.get("slug") != slug or data.get("candidate_digest") != digest(candidate):
        raise CoordinationError("merge evidence does not match candidate")
    repositories = data.get("repositories")
    if not isinstance(repositories, list) or len(repositories) != len(candidate["repositories"]):
        raise CoordinationError("merge repository set is invalid")
    for expected, entry in zip(candidate["repositories"], repositories):
        if not isinstance(entry, dict) or set(entry) != {
            "path", "target_ref", "target_sha", "candidate_sha", "merge_sha"
        }:
            raise CoordinationError("merge repository entry is invalid")
        if entry["path"] != expected["path"] or entry["target_ref"] != expected["target_ref"] or entry["candidate_sha"] != expected["candidate_sha"]:
            raise CoordinationError("merge repository does not match candidate")
        if SHA_RE.fullmatch(str(entry["target_sha"])) is None or SHA_RE.fullmatch(str(entry["merge_sha"])) is None:
            raise CoordinationError("merge repository SHA is invalid")
    return data


def validate_task_records(data: Any) -> list[dict[str, Any]]:
    required = {"name", "repository", "argv", "exit_status", "started_at", "ended_at", "artifacts"}
    if not isinstance(data, list) or not data:
        raise CoordinationError("task-plan validation requires command records")
    result = []
    for record in data:
        if not isinstance(record, dict) or set(record) != required:
            raise CoordinationError("task-plan command record fields are invalid")
        if not isinstance(record["name"], str) or not record["name"] or not isinstance(record["repository"], str) or not record["repository"]:
            raise CoordinationError("task-plan command record identity is invalid")
        if not isinstance(record["argv"], list) or not record["argv"] or not all(isinstance(arg, str) and arg for arg in record["argv"]):
            raise CoordinationError("task-plan command argv is invalid")
        if type(record["exit_status"]) is not int or not isinstance(record["started_at"], str) or not record["started_at"] or not isinstance(record["ended_at"], str) or not record["ended_at"]:
            raise CoordinationError("task-plan command result is invalid")
        if not isinstance(record["artifacts"], list) or not all(isinstance(path, str) and path for path in record["artifacts"]):
            raise CoordinationError("task-plan artifacts are invalid")
        result.append(dict(record))
    return result


def same_validation(existing: Any, current: dict[str, Any]) -> bool:
    if existing == current:
        return True
    if not isinstance(existing, dict):
        return False
    previous = dict(existing)
    candidate = dict(current)
    previous_definition = previous.get("definition")
    candidate_definition = candidate.get("definition")
    if (
        isinstance(previous_definition, dict)
        and isinstance(candidate_definition, dict)
        and previous_definition.get("kind") == "task_plan"
        and candidate_definition.get("kind") == "task_plan"
    ):
        previous["definition"] = {
            key: value for key, value in previous_definition.items()
            if key != "task_revision"
        }
        candidate["definition"] = {
            key: value for key, value in candidate_definition.items()
            if key != "task_revision"
        }
    return previous == candidate


def _record_validation(
    workspace: Path,
    slug: str,
    verdict: str | None,
    task_plan_records: Path | None,
) -> None:
    with mutation_guard(workspace) as (control, expected_sha):
        buckets, readmes, current = task_claim(workspace, slug)
        if buckets.get(slug) != "Authoring":
            raise CoordinationError("validation requires an Authoring task")
        candidate = load_candidate(workspace, slug)
        verify_candidate_refs(workspace, slug, candidate)
        if task_plan_records is None or verdict is None:
            raise CoordinationError("task-plan validation arguments are required")
        records = validate_task_records(read_json(task_plan_records))
        if verdict == "pass" and any(record["exit_status"] != 0 for record in records):
            raise CoordinationError("passing task-plan validation contains a failed command")
        definition = {
            "kind": "task_plan",
            "task_revision": git(workspace, "rev-parse", "HEAD").stdout.strip(),
            "task_path": readmes[slug],
            "task_digest": hashlib.sha256(
                (workspace / readmes[slug]).read_bytes()
            ).hexdigest(),
        }
        outcome = verdict
        checks = records
        data = {
            "slug": slug,
            "candidate_digest": digest(candidate),
            "definition": definition,
            "verdict": outcome,
            "checks": checks,
        }
        paths = evidence_paths(workspace, slug)
        changed = [paths["validation.json"]]
        if paths["validation.json"].exists() and same_validation(
            read_json(paths["validation.json"]), data
        ):
            print(f"VALIDATED\t{slug}\t{outcome}")
            return
        write_json(paths["validation.json"], data)
        relative = [relative_git_path(path, workspace) for path in changed]
        commit_and_push(workspace, control, expected_sha, f"Record validation for {slug}", relative)
        print(f"VALIDATED\t{slug}\t{outcome}")


def record_validation(
    workspace: Path,
    slug: str,
    verdict: str | None,
    task_plan_records: Path | None,
) -> None:
    _record_validation(workspace, slug, verdict, task_plan_records)


def load_validation(
    workspace: Path, slug: str, candidate: dict[str, Any]
) -> dict[str, Any]:
    data = read_json(evidence_paths(workspace, slug)["validation.json"])
    required = {"slug", "candidate_digest", "definition", "verdict", "checks"}
    if not isinstance(data, dict) or set(data) != required:
        raise CoordinationError("validation evidence fields are invalid")
    if data["slug"] != slug or data["candidate_digest"] != digest(candidate) or data["verdict"] != "pass":
        raise CoordinationError("validation evidence is not a matching pass")
    definition = data["definition"]
    if not isinstance(definition, dict) or definition.get("kind") != "task_plan":
        raise CoordinationError("validation definition is invalid")
    return data


def validation_binding_current(
    workspace: Path, candidate: dict[str, Any], validation: dict[str, Any]
) -> None:
    definition = validation["definition"]
    revision = definition.get("task_revision")
    if SHA_RE.fullmatch(str(revision)) is None or git(workspace, "merge-base", "--is-ancestor", str(revision), "HEAD", check=False).returncode:
        raise CoordinationError("task-plan validation revision is stale")
    task_path = definition.get("task_path")
    task_digest = definition.get("task_digest")
    if (
        not isinstance(task_path, str)
        or not task_path.startswith("scratch/")
        or hashlib.sha256((workspace / task_path).read_bytes()).hexdigest()
        != task_digest
    ):
        raise CoordinationError("task-plan validation definition changed")


def publish_completion(
    workspace: Path,
    control: ControlBranch,
    expected_task_tree: str,
    slug: str,
) -> None:
    for _ in range(10):
        tree = fetch_ref(workspace, control.ref)
        buckets, _ = parse_todo(text_from_tree(workspace, tree, "TODO.md"))
        if buckets.get(slug) != "Authoring":
            raise PublicationError("task changed during target publication")
        current_task_tree = git(
            workspace, "rev-parse", f"{tree}:scratch/{slug}"
        ).stdout.strip()
        if current_task_tree != expected_task_tree:
            raise PublicationError("task changed during target publication")
        dependencies = manifest_from_tree(
            workspace, tree, slug, require_ready=True
        )["dependencies"]
        unresolved = [
            dependency for dependency in dependencies
            if buckets.get(dependency) != "Done"
        ]
        if unresolved:
            raise PublicationError(
                f"task {slug} has unresolved dependencies after target publication: "
                f"{','.join(unresolved)}"
            )
        with tempfile.TemporaryDirectory(prefix="task-complete-") as directory:
            temporary = Path(directory)
            git(workspace, "worktree", "add", "--detach", str(temporary), tree)
            try:
                move_todo(temporary, slug, "Authoring", "Done")
                claim_path = temporary / "scratch" / slug / "claim.json"
                claim_path.unlink()
                git(temporary, "add", "TODO.md", str(claim_path.relative_to(temporary)))
                git(temporary, "commit", "-m", f"Complete task {slug}")
                generated = git(temporary, "rev-parse", "HEAD").stdout.strip()
                pushed = push_control_ref(temporary, control, tree)
            finally:
                git(workspace, "worktree", "remove", "--force", str(temporary))
        observed = fetch_ref(workspace, control.ref)
        observed_buckets, _ = parse_todo(
            text_from_tree(workspace, observed, "TODO.md")
        )
        observed_claims = claims_from_tree(
            workspace, observed, observed_buckets, strict=False
        )
        if (
            observed_buckets.get(slug) == "Done"
            and not any(claim["slug"] == slug for claim in observed_claims)
            and git(
                workspace, "merge-base", "--is-ancestor", generated, observed,
                check=False,
            ).returncode == 0
        ):
            synchronize_checkout(workspace, control, observed)
            return
        if pushed.returncode == 0:
            raise PublicationError("published completion is missing from remote state")
        if observed == tree:
            detail = pushed.stderr.strip() or pushed.stdout.strip()
            raise PublicationError(f"completion push failed: {detail}")
    raise PublicationError("completion did not converge after concurrent updates")


def restore_target_checkout(repo: Path, target_ref: str, target_sha: str) -> None:
    branch = target_ref.removeprefix("refs/heads/")
    ensure_clean(repo)
    current = git(repo, "rev-parse", "HEAD").stdout.strip()
    if git(
        repo, "merge-base", "--is-ancestor", current, target_sha, check=False
    ).returncode:
        raise CoordinationError("child checkout contains unpublished work")
    local = git(repo, "rev-parse", f"refs/heads/{branch}", check=False)
    if local.returncode:
        git(repo, "branch", branch, target_sha)
    if current_branch(repo) != branch:
        git(repo, "checkout", branch)
    local_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    if git(
        repo, "merge-base", "--is-ancestor", local_sha, target_sha, check=False
    ).returncode:
        raise CoordinationError(f"child checkout diverged from {target_ref}")
    if local_sha != target_sha:
        git(repo, "merge", "--ff-only", target_sha)
    ensure_clean(repo)


def cleanup_task_refs(
    workspace: Path,
    slug: str,
    candidate: dict[str, Any],
    merge: dict[str, Any],
    *,
    preserve_unpublished_candidates: bool = False,
) -> None:
    candidate_ref = f"refs/heads/candidate/{slug}"
    merge_ref = f"refs/heads/validation/{slug}"
    for candidate_entry_data, merge_entry in zip(
        candidate["repositories"], merge["repositories"]
    ):
        _, repo = normalized_repository(workspace, candidate_entry_data["path"])
        try:
            integrated = True
            if preserve_unpublished_candidates:
                target = remote_sha(repo, merge_entry["target_ref"])
                integrated = target is not None and not git(
                    repo, "merge-base", "--is-ancestor",
                    candidate_entry_data["candidate_sha"], target, check=False,
                ).returncode
            for ref, expected in (
                (candidate_ref, candidate_entry_data["candidate_sha"]),
                (merge_ref, merge_entry["merge_sha"]),
            ):
                if ref == candidate_ref and not integrated:
                    continue
                if remote_sha(repo, ref) == expected:
                    git(
                        repo, "push", f"--force-with-lease={ref}:{expected}",
                        "origin", f":{ref}", check=False,
                    )
        except CoordinationError:
            pass


def _complete(workspace: Path, slug: str) -> None:
    with mutation_guard(workspace) as (control, expected_sha):
        buckets, _, current = task_claim(workspace, slug)
        if buckets.get(slug) != "Authoring":
            raise CoordinationError("completion requires an Authoring task")
        dependencies = local_manifest(workspace, slug, require_ready=True)["dependencies"]
        unresolved = [
            dependency for dependency in dependencies
            if buckets.get(dependency) != "Done"
        ]
        if unresolved:
            raise CoordinationError(
                f"task {slug} has unresolved dependencies: {','.join(unresolved)}"
            )
        candidate = load_candidate(workspace, slug)
        merge = load_merge(workspace, slug, candidate)
        validation = load_validation(workspace, slug, candidate)
        expected_task_tree = git(
            workspace, "rev-parse", f"{expected_sha}:scratch/{slug}"
        ).stdout.strip()
        validation_binding_current(workspace, candidate, validation)
        merge_ref = f"refs/heads/validation/{slug}"
        for candidate_entry_data, merge_entry in zip(
            candidate["repositories"], merge["repositories"]
        ):
            _, repo = normalized_repository(workspace, candidate_entry_data["path"])
            if remote_sha(
                repo, f"refs/heads/candidate/{slug}"
            ) != candidate_entry_data["candidate_sha"]:
                raise CoordinationError(
                    f"candidate changed: {candidate_entry_data['path']}"
                )
            observed_target = remote_sha(repo, merge_entry["target_ref"])
            if observed_target is not None and not git(
                repo, "merge-base", "--is-ancestor",
                candidate_entry_data["candidate_sha"], observed_target,
                check=False,
            ).returncode:
                continue
            if observed_target != merge_entry["target_sha"]:
                raise TargetMoved(candidate_entry_data["path"])
            if remote_sha(repo, merge_ref) != merge_entry["merge_sha"]:
                raise CoordinationError(
                    f"prepared merge changed: {candidate_entry_data['path']}"
                )
            pushed = git(
                repo, "push", "origin",
                f"{merge_entry['merge_sha']}:{merge_entry['target_ref']}", check=False,
            )
            observed_target = remote_sha(repo, merge_entry["target_ref"])
            if observed_target is not None and not git(
                repo, "merge-base", "--is-ancestor",
                candidate_entry_data["candidate_sha"], observed_target,
                check=False,
            ).returncode:
                continue
            if observed_target != merge_entry["target_sha"]:
                raise TargetMoved(candidate_entry_data["path"])
            if observed_target != merge_entry["merge_sha"]:
                detail = pushed.stderr.strip() or pushed.stdout.strip()
                raise CoordinationError(
                    f"target publication failed: {candidate_entry_data['path']}: {detail}"
                )
        for candidate_entry_data, merge_entry in zip(
            candidate["repositories"], merge["repositories"]
        ):
            _, repo = normalized_repository(workspace, candidate_entry_data["path"])
            if remote_sha(
                repo, f"refs/heads/candidate/{slug}"
            ) != candidate_entry_data["candidate_sha"]:
                raise CoordinationError(
                    f"candidate changed: {candidate_entry_data['path']}"
                )
            target_sha = fetch_ref(repo, merge_entry["target_ref"])
            if git(
                repo, "merge-base", "--is-ancestor",
                candidate_entry_data["candidate_sha"], target_sha,
                check=False,
            ).returncode:
                raise TargetMoved(candidate_entry_data["path"])
            restore_target_checkout(
                repo, merge_entry["target_ref"], target_sha
            )
        publish_completion(workspace, control, expected_task_tree, slug)
        cleanup_task_refs(workspace, slug, candidate, merge)
        print(f"COMPLETED\t{slug}\tDone")


def complete(workspace: Path, slug: str) -> None:
    while True:
        try:
            _prepare(workspace, slug)
            _complete(workspace, slug)
            return
        except TargetMoved:
            continue
        except MergeConflict as exc:
            paths = evidence_paths(workspace, slug)
            if paths["merge.json"].exists():
                candidate = load_candidate(workspace, slug)
                merge = load_merge(workspace, slug, candidate)
                cleanup_task_refs(
                    workspace, slug, candidate, merge,
                    preserve_unpublished_candidates=True,
                )
            print(f"CONFLICT\t{slug}\tAuthoring\t{exc}")
            return


def local_manifest(workspace: Path, slug: str, *, require_ready: bool = False) -> dict[str, Any]:
    path = task_dir(workspace, slug) / "task.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CoordinationError(f"task manifest is missing: {slug}") from exc
    except json.JSONDecodeError as exc:
        raise CoordinationError(f"invalid JSON in task manifest: {slug}: {exc}") from exc
    return validate_manifest(data, slug, require_ready=require_ready)


def validate_local_dependencies(workspace: Path, buckets: dict[str, str]) -> None:
    graph: dict[str, list[str]] = {}
    for slug in buckets:
        path = workspace / "scratch" / slug / "task.json"
        if not path.exists():
            continue
        manifest = local_manifest(workspace, slug)
        if manifest["dependencies"] is not None:
            graph[slug] = manifest["dependencies"]
    for slug, dependencies in graph.items():
        for dependency in dependencies:
            if dependency == slug:
                raise CoordinationError(f"task depends on itself: {slug}")
            if dependency not in buckets:
                raise CoordinationError(f"task dependency is missing: {dependency}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(slug: str) -> None:
        if slug in visiting:
            raise CoordinationError("task dependencies contain a cycle")
        if slug in visited:
            return
        visiting.add(slug)
        for dependency in graph.get(slug, []):
            visit(dependency)
        visiting.remove(slug)
        visited.add(slug)

    for slug in graph:
        visit(slug)


def create(workspace: Path, slug: str) -> None:
    validate_new_slug(slug)
    with mutation_guard(workspace) as (control, expected_sha):
        buckets, _ = parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
        if slug in buckets or (workspace / "scratch" / slug).exists():
            raise CoordinationError(f"task already exists: {slug}")
        directory = workspace / "scratch" / slug
        directory.mkdir(parents=True)
        (directory / "README.md").write_text(f"# {slug}\n", encoding="utf-8")
        (directory / "JOURNAL.md").write_text("# Task Journal\n", encoding="utf-8")
        write_json(directory / "task.json", {
            "dependencies": None,
            "capabilities": None,
            "resources": None,
            "repositories": None,
        })
        insert_task(workspace, slug, "Backlog")
        parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
        commit_and_push(
            workspace, control, expected_sha, f"Create task {slug}",
            [
                "TODO.md",
                f"scratch/{slug}/README.md",
                f"scratch/{slug}/JOURNAL.md",
                f"scratch/{slug}/task.json",
            ],
        )
    print(f"CREATED\t{slug}\tBacklog")


def create_follow_up(workspace: Path, source: str, slug: str) -> None:
    validate_new_slug(slug)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-follow-up-[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise CoordinationError("follow-up slug must use YYYY-MM-DD-follow-up-description")
    note_relative = f"scratch/{source}/follow-up.md"
    with task_edit_guard(workspace, {note_relative}) as (control, expected_sha):
        buckets, _ = parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
        task_claim(workspace, source)
        if slug in buckets or (workspace / "scratch" / slug).exists():
            raise CoordinationError(f"task already exists: {slug}")
        note = nonempty_text(workspace / note_relative, "follow-up")
        directory = workspace / "scratch" / slug
        directory.mkdir(parents=True)
        (directory / "README.md").write_text(
            f"# Follow-up from `{source}`\n\n{note}\n", encoding="utf-8"
        )
        (directory / "JOURNAL.md").write_text("# Task Journal\n", encoding="utf-8")
        write_json(directory / "task.json", {
            "dependencies": None,
            "capabilities": None,
            "resources": None,
            "repositories": None,
        })
        (workspace / note_relative).unlink()
        insert_task(workspace, slug, "Backlog")
        commit_and_push(
            workspace, control, expected_sha, f"Create follow-up task {slug}",
            [
                "TODO.md", note_relative,
                f"scratch/{slug}/README.md",
                f"scratch/{slug}/JOURNAL.md",
                f"scratch/{slug}/task.json",
            ],
        )
    print(f"FOLLOW_UP\t{source}\t{slug}\tBacklog")


def check(workspace: Path, slug: str) -> None:
    buckets, _ = parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
    if slug not in buckets:
        raise CoordinationError(f"task is missing from TODO: {slug}")
    local_manifest(workspace, slug, require_ready=True)
    journal_path = task_dir(workspace, slug) / "JOURNAL.md"
    try:
        journal = journal_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CoordinationError(f"task Journal is missing: {slug}") from exc
    journal_entry_numbers(journal, slug)
    validate_local_dependencies(workspace, buckets)
    print(f"READY\t{slug}")


def task_edit_guard(
    workspace: Path,
    allowed_dirty: set[str],
):
    @contextmanager
    def guard():
        with coordination_lock(workspace):
            actual = changed_paths(workspace)
            if not actual.issubset(allowed_dirty):
                raise PublicationError(
                    f"unrelated local changes: {sorted(actual - allowed_dirty)}"
                )
            control = resolve_default_branch(workspace)
            if current_branch(workspace) != control.name:
                raise PublicationError(
                    f"queue checkout must be on control branch {control.name}"
                )
            remote = fetch_ref(workspace, control.ref)
            local = git(workspace, "rev-parse", "HEAD").stdout.strip()
            if local != remote:
                if git(
                    workspace, "merge-base", "--is-ancestor", local, remote,
                    check=False,
                ).returncode:
                    raise PublicationError("queue checkout diverged from published state")
                remote_changes = set(
                    git(workspace, "diff", "--name-only", local, remote).stdout.splitlines()
                )
                conflicts = remote_changes.intersection(allowed_dirty)
                if conflicts:
                    raise PublicationError(
                        f"task changed remotely: {sorted(conflicts)}"
                    )
                git(workspace, "merge", "--ff-only", remote)
                actual = changed_paths(workspace)
                if not actual.issubset(allowed_dirty):
                    raise PublicationError(
                        f"unrelated local changes after synchronization: "
                        f"{sorted(actual - allowed_dirty)}"
                    )
            yield control, remote
    return guard()


def ready(workspace: Path, slug: str, *, emit: bool = True) -> None:
    manifest_path = f"scratch/{slug}/task.json"
    readme_path = f"scratch/{slug}/README.md"
    journal_path = f"scratch/{slug}/JOURNAL.md"
    with task_edit_guard(
        workspace,
        {manifest_path, readme_path, journal_path},
    ) as (control, expected_sha):
        buckets, _ = parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
        source = buckets.get(slug)
        if source != "Backlog":
            raise CoordinationError(f"task {slug} cannot become Ready from {source}")
        local_manifest(workspace, slug, require_ready=True)
        validate_local_dependencies(workspace, buckets)
        changed = ["TODO.md", manifest_path, readme_path, journal_path]
        move_todo(workspace, slug, source, "Ready")
        commit_and_push(workspace, control, expected_sha, f"Ready task {slug}", changed)
    if emit:
        print(f"READY\t{slug}")


def reorder(workspace: Path, slug: str, peer: str, before: bool) -> None:
    with mutation_guard(workspace) as (control, expected_sha):
        buckets, _ = parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
        bucket = buckets.get(slug)
        if bucket != "Ready" or buckets.get(peer) != bucket:
            raise CoordinationError("reorder requires two tasks in the Ready queue")
        path = workspace / "TODO.md"
        lines = path.read_text(encoding="utf-8").splitlines()
        task_index = next(i for i, line in enumerate(lines) if (m := TASK_RE.match(line)) and m.group(1) == slug)
        task_line = lines.pop(task_index)
        peer_index = next(i for i, line in enumerate(lines) if (m := TASK_RE.match(line)) and m.group(1) == peer)
        lines.insert(peer_index if before else peer_index + 1, task_line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        commit_and_push(workspace, control, expected_sha, f"Reorder task {slug}", ["TODO.md"])
    print(f"REORDERED\t{slug}\t{'before' if before else 'after'}\t{peer}")


def dependency(workspace: Path, action: str, slug: str, dependency_slug: str | None) -> None:
    with mutation_guard(workspace) as (control, expected_sha):
        buckets, _ = parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
        if slug not in buckets:
            raise CoordinationError(f"task is missing from TODO: {slug}")
        if buckets[slug] in {"Done", "Cancelled"}:
            raise CoordinationError(f"terminal task dependencies cannot change: {slug}")
        manifest = local_manifest(workspace, slug)
        values = manifest["dependencies"]
        changed = False
        if action == "clear":
            changed = values != []
            manifest["dependencies"] = []
        elif action == "add":
            assert dependency_slug is not None
            if dependency_slug == slug or dependency_slug not in buckets:
                raise CoordinationError(f"invalid task dependency: {dependency_slug}")
            if values is None:
                manifest["dependencies"] = [dependency_slug]
                changed = True
            elif dependency_slug not in values:
                values.append(dependency_slug)
                changed = True
        else:
            assert dependency_slug is not None
            if values is not None and dependency_slug in values:
                values.remove(dependency_slug)
                changed = True
        if not changed:
            print(f"UNCHANGED\t{slug}\tdependencies")
            return
        path = task_dir(workspace, slug) / "task.json"
        write_json(path, manifest)
        validate_local_dependencies(workspace, buckets)
        commit_and_push(
            workspace, control, expected_sha, f"Update dependencies for {slug}",
            [relative_git_path(path, workspace)],
        )
    print(f"UPDATED\t{slug}\tdependencies")


def nonempty_text(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise CoordinationError(f"{label} is missing: {path.name}") from exc
    if not value:
        raise CoordinationError(f"{label} is empty: {path.name}")
    return value


def unfenced_markdown_lines(lines: list[str]) -> Iterable[str]:
    fence_character = ""
    fence_length = 0
    for line in lines:
        match = MARKDOWN_FENCE_RE.fullmatch(line)
        if fence_character:
            if match:
                marker, remainder = match.groups()
                if (
                    marker[0] == fence_character
                    and len(marker) >= fence_length
                    and not remainder.strip()
                ):
                    fence_character = ""
                    fence_length = 0
                    yield ""
            continue
        if match:
            marker, remainder = match.groups()
            if marker[0] == "`" and "`" in remainder:
                yield line
                continue
            fence_character = marker[0]
            fence_length = len(marker)
            yield ""
            continue
        yield line


def journal_entries(lines: list[str], slug: str) -> list[tuple[int, list[str]]]:
    if not lines or lines[0] != "# Task Journal":
        raise CoordinationError(f"task Journal must begin with '# Task Journal': {slug}")
    entries: list[tuple[int, list[str]]] = []
    for line in unfenced_markdown_lines(lines[1:]):
        match = JOURNAL_ENTRY_RE.fullmatch(line)
        if match:
            entries.append((int(match.group(1)), []))
        elif MARKDOWN_H1_H2_RE.match(line):
            raise CoordinationError(
                f"task Journal contains a non-entry H1 or H2 heading: {slug}"
            )
        elif entries:
            entries[-1][1].append(line)
    numbers = [number for number, _ in entries]
    if len(numbers) != len(set(numbers)) or numbers != sorted(numbers, reverse=True):
        raise CoordinationError(
            f"task Journal entries must be unique and newest first: {slug}"
        )
    return entries


def journal_entry_numbers(lines: list[str], slug: str) -> list[int]:
    return [number for number, _ in journal_entries(lines, slug)]


def validate_journal_fragment(value: str, label: str) -> None:
    lines = list(unfenced_markdown_lines(value.splitlines()))
    for index, line in enumerate(lines):
        if MARKDOWN_H1_H2_RE.match(line):
            raise CoordinationError(
                f"{label} must be a Markdown fragment without H1 or H2 headings"
            )
        if (
            index
            and lines[index - 1].strip()
            and MARKDOWN_SETEXT_RE.fullmatch(line)
        ):
            raise CoordinationError(
                f"{label} must be a Markdown fragment without H1 or H2 headings"
            )


def record_journal_entry(
    workspace: Path,
    slug: str,
    notes: list[tuple[str, str, str]],
) -> list[str]:
    directory = task_dir(workspace, slug)
    journal_path = directory / "JOURNAL.md"
    values = [
        (heading, directory / filename, nonempty_text(directory / filename, label))
        for heading, filename, label in notes
    ]
    for _, _, value in values:
        validate_journal_fragment(value, "journal entry")
    try:
        lines = journal_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CoordinationError(f"task Journal is missing: {slug}") from exc
    numbers = journal_entry_numbers(lines, slug)
    number = (numbers[0] if numbers else 0) + 1
    entry = [
        f"## Entry {number:04d}",
        "",
        f"**{values[0][0]}**",
        "",
        f"**Recorded:** {datetime.now().astimezone().date().isoformat()}",
        "",
        values[0][2],
        "",
    ]
    for heading, _, value in values[1:]:
        entry.extend((f"### {heading}", "", value, ""))
    existing = lines[1:]
    while existing and not existing[0]:
        existing.pop(0)
    journal_path.write_text(
        "\n".join([lines[0], "", *entry, *existing]).rstrip() + "\n",
        encoding="utf-8",
    )
    for _, path, _ in values:
        path.unlink()
    return [
        relative_git_path(journal_path, workspace),
        *(relative_git_path(path, workspace) for _, path, _ in values),
    ]


def checkpoint_numbers(workspace: Path, slug: str) -> list[int]:
    path = task_dir(workspace, slug) / "JOURNAL.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CoordinationError(f"task Journal is missing: {slug}") from exc
    return [
        number for number, body in journal_entries(lines, slug)
        if next((line for line in body if line), "") == "**Checkpoint**"
    ]


def checkpoint_intent_path(workspace: Path, slug: str) -> Path:
    common = Path(git(workspace, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = workspace / common
    return common / "nk" / "checkpoints" / f"{slug}.json"


def checkpoint_intent(workspace: Path, slug: str, digest_value: str) -> tuple[Path, str]:
    path = checkpoint_intent_path(workspace, slug)
    data = read_json(path) if path.exists() else None
    if (
        isinstance(data, dict)
        and set(data) == {"operation_id", "digest"}
        and CHECKPOINT_ID_RE.fullmatch(
            f"<!-- nk-checkpoint-id: {data['operation_id']} {data['digest']} -->"
        )
        and data["digest"] == digest_value
    ):
        return path, data["operation_id"]
    operation_id = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    write_json(temporary, {"operation_id": operation_id, "digest": digest_value})
    os.replace(temporary, path)
    return path, operation_id


def journal_has_checkpoint_id(
    workspace: Path, slug: str, operation_id: str, digest_value: str,
) -> bool:
    path = task_dir(workspace, slug) / "JOURNAL.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CoordinationError(f"task Journal is missing: {slug}") from exc
    marker = f"<!-- nk-checkpoint-id: {operation_id} {digest_value} -->"
    return any(
        next((line for line in body if line), "") == "**Checkpoint**"
        and marker in body
        for _, body in journal_entries(lines, slug)
    )


def ignore_local_path(workspace: Path, relative: str) -> None:
    exclude = Path(git(workspace, "rev-parse", "--git-path", "info/exclude").stdout.strip())
    if not exclude.is_absolute():
        exclude = workspace / exclude
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = text.splitlines()
    if relative in lines:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not text or text.endswith("\n") else "\n"
    exclude.write_text(f"{text}{separator}{relative}\n", encoding="utf-8")


def parse_resume_after(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise CoordinationError("resume-after must be an ISO 8601 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CoordinationError(
            "resume-after must be an ISO 8601 timestamp with timezone"
        ) from exc
    if parsed.tzinfo is None:
        raise CoordinationError("resume-after must be an ISO 8601 timestamp with timezone")
    return parsed.astimezone(timezone.utc)


def checkpoint_companion_commits(
    workspace: Path, slug: str, remote: str,
) -> tuple[str, list[str]]:
    local = git(workspace, "rev-parse", "HEAD").stdout.strip()
    common = git(workspace, "merge-base", local, remote).stdout.strip()
    revision_range = f"{common}..{local}"
    merges = git(
        workspace, "rev-list", "--merges", revision_range,
    ).stdout.splitlines()
    if merges:
        raise CoordinationError(
            "checkpoint local companion history contains merge commits; "
            "leave queue reconciliation to nk task checkpoint: " + ", ".join(merges)
        )
    output = git(
        workspace, "log", "--no-renames", "--format=", "--name-only", "-z",
        revision_range,
    ).stdout
    paths = sorted(set(filter(None, output.split("\0"))))
    protected = {
        pattern.replace("<slug>", slug): (reason, owner)
        for pattern, reason, owner in CHECKPOINT_PROTECTED_PATTERNS
    }
    prefix = f"scratch/{slug}/"
    offending = [path for path in paths if not path.startswith(prefix) or path in protected]
    if not offending:
        return local, paths

    details = []
    for path in offending:
        if path in protected:
            reason, owner = protected[path]
            details.append(f"- {path}: protected {reason}; owned by {owner}")
        else:
            details.append(f"- {path}: outside the claimed task directory {prefix}")
    patterns = [
        f"- {pattern}: {reason}; owned by {owner}"
        for pattern, reason, owner in CHECKPOINT_PROTECTED_PATTERNS
    ]
    raise CoordinationError(
        "checkpoint local commits contain paths that cannot be published:\n"
        + "\n".join(details)
        + "\nProtected file patterns:\n"
        + "\n".join(patterns)
        + f"\nCommit only unmanaged companion files under {prefix}"
    )


def checkpoint(workspace: Path, slug: str, resume_after: str | None = None) -> None:
    normalized_resume = (
        parse_resume_after(resume_after).isoformat().replace("+00:00", "Z")
        if resume_after is not None else None
    )
    relative = f"scratch/{slug}/progress.md"
    progress = workspace / relative
    with coordination_lock(workspace):
        ignore_local_path(workspace, relative)
        ensure_clean(workspace)
        control = resolve_default_branch(workspace)
        if current_branch(workspace) != control.name:
            raise CoordinationError(f"queue checkout must be on control branch {control.name}")
        observed = git(workspace, "rev-parse", tracking_ref(control.ref)).stdout.strip()
        local, _ = checkpoint_companion_commits(workspace, slug, observed)
        buckets, _, claim = task_claim(workspace, slug)
        if buckets.get(slug) != "Authoring":
            raise CoordinationError("checkpoint requires an Authoring task")
        ensure_claim_repositories_clean(workspace, claim)
        remote = fetch_ref(workspace, control.ref)
        local = git(workspace, "rev-parse", "HEAD").stdout.strip()
        if not git(
            workspace, "merge-base", "--is-ancestor", local, remote, check=False,
        ).returncode:
            synchronize_checkout(workspace, control, remote)
        local, _ = checkpoint_companion_commits(
            workspace, slug, remote
        )
        pre_rebase = None
        if git(
            workspace, "merge-base", "--is-ancestor", remote, local, check=False,
        ).returncode:
            pre_rebase = local
            rebased = git(workspace, "rebase", remote, check=False)
            if rebased.returncode:
                conflicts = status_lines(workspace)
                git(workspace, "rebase", "--abort", check=False)
                raise CoordinationError(
                    "checkpoint companion commits conflict with the published queue; "
                    "the rebase was aborted and local commits plus progress.md are preserved:\n"
                    + "\n".join(conflicts)
                )
            local = git(workspace, "rev-parse", "HEAD").stdout.strip()
            try:
                checkpoint_companion_commits(workspace, slug, remote)
            except CoordinationError:
                git(workspace, "reset", "--hard", pre_rebase)
                raise
        try:
            buckets, _, current_claim = task_claim(workspace, slug)
            if buckets.get(slug) != "Authoring":
                raise CoordinationError("checkpoint requires an Authoring task")
            if current_claim["claim_id"] != claim["claim_id"]:
                raise CoordinationError("task claim changed before Checkpoint publication")
        except CoordinationError:
            if pre_rebase is not None:
                git(workspace, "reset", "--hard", pre_rebase)
            raise
        fragment = nonempty_text(progress, "progress")
        validate_journal_fragment(fragment, "progress")
        digest_value = hashlib.sha256(
            f"{normalized_resume or ''}\0{fragment}".encode()
        ).hexdigest()
        intent, operation_id = checkpoint_intent(workspace, slug, digest_value)
        marker = f"<!-- nk-checkpoint-id: {operation_id} {digest_value} -->"
        if journal_has_checkpoint_id(workspace, slug, operation_id, digest_value):
            progress.unlink()
            intent.unlink(missing_ok=True)
            print(f"CHECKPOINTED\t{slug}\tAuthoring")
            return
        with tempfile.TemporaryDirectory(prefix="task-checkpoint-") as directory:
            temporary = Path(directory)
            git(workspace, "worktree", "add", "--detach", str(temporary), local)
            try:
                merged_buckets, _ = parse_todo(
                    (temporary / "TODO.md").read_text(encoding="utf-8"), temporary
                )
                claim_path = temporary / "scratch" / slug / "claim.json"
                merged_claim = validate_claim(
                    read_json(claim_path), relative_git_path(claim_path, temporary),
                    merged_buckets,
                )
                if merged_claim["claim_id"] != current_claim["claim_id"]:
                    raise CoordinationError("task claim changed before Checkpoint publication")
                note = temporary / relative
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text(f"{marker}\n\n{fragment.rstrip()}\n", encoding="utf-8")
                claim_data = read_json(claim_path)
                if normalized_resume is None:
                    claim_data.pop("resume_after", None)
                else:
                    claim_data["resume_after"] = normalized_resume
                write_json(claim_path, claim_data)
                changed = record_journal_entry(
                    temporary, slug, [("Checkpoint", "progress.md", "progress")]
                )
                changed.append(relative_git_path(claim_path, temporary))
                commit_and_push(
                    temporary, control, remote, f"Checkpoint task {slug}", changed
                )
            finally:
                git(workspace, "worktree", "remove", "--force", str(temporary), check=False)
        observed = git(workspace, "rev-parse", tracking_ref(control.ref)).stdout.strip()
        synchronize_checkout(workspace, control, observed)
        progress.unlink()
        intent.unlink(missing_ok=True)
    print(f"CHECKPOINTED\t{slug}\tAuthoring")


def record_resolution(workspace: Path, slug: str) -> list[str]:
    return record_journal_entry(workspace, slug, [
        ("Resolution", "resolution.md", "resolution"),
        ("Resolved blocker", "blocker.md", "blocker"),
    ])


def cancellation_intent_path(workspace: Path, slug: str) -> Path:
    common = Path(git(workspace, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = workspace / common
    return common / "nk" / "cancellations" / f"{slug}.json"


def cancellation_was_published(
    workspace: Path, intent: Path, digest_value: str, remote: str,
) -> bool:
    data = read_json(intent) if intent.exists() else None
    return bool(
        isinstance(data, dict)
        and set(data) == {"digest", "commit"}
        and data["digest"] == digest_value
        and SHA_RE.fullmatch(str(data["commit"]))
        and git(
            workspace, "merge-base", "--is-ancestor", data["commit"], remote,
            check=False,
        ).returncode == 0
    )


def apply_cancellation(workspace: Path, slug: str, source: str) -> None:
    notes = [("Cancellation", "cancellation.md", "cancellation")]
    if source == "Blocked":
        notes.append(("Existing blocker", "blocker.md", "blocker"))
    record_journal_entry(workspace, slug, notes)
    buckets, _ = parse_todo(
        (workspace / "TODO.md").read_text(encoding="utf-8"), workspace
    )
    if source == "Authoring":
        claim_path = task_dir(workspace, slug) / "claim.json"
        validate_claim(
            read_json(claim_path), relative_git_path(claim_path, workspace), buckets
        )
        claim_path.unlink()
    remove_evidence(evidence_paths(workspace, slug), EVIDENCE_NAMES)
    move_todo(workspace, slug, source, "Cancelled")
    for dependent, bucket in list(buckets.items()):
        if bucket in {"Done", "Cancelled", "Blocked"}:
            continue
        manifest_path = workspace / "scratch" / dependent / "task.json"
        if not manifest_path.exists():
            continue
        manifest = local_manifest(workspace, dependent)
        if slug not in (manifest["dependencies"] or []):
            continue
        # An active author owns this route. Preserve its claim and local commits;
        # its next turn can checkpoint before choosing the resulting route.
        if bucket == "Authoring":
            continue
        dependent_dir = task_dir(workspace, dependent)
        blocker_path = dependent_dir / "blocker.md"
        blocker_path.write_text(
            f"Dependency `{slug}` was cancelled.\n", encoding="utf-8"
        )
        move_todo(workspace, dependent, bucket, "Blocked")


def block(workspace: Path, slug: str) -> None:
    blocker_relative = f"scratch/{slug}/blocker.md"
    with task_edit_guard(
        workspace, {blocker_relative}
    ) as (control, expected_sha):
        buckets, _ = parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
        source = buckets.get(slug)
        if source is None:
            raise CoordinationError(f"task is missing from TODO: {slug}")
        if source in {"Done", "Cancelled"}:
            raise CoordinationError(f"terminal task cannot be blocked: {slug}")
        nonempty_text(workspace / blocker_relative, "blocker")
        changed = ["TODO.md", blocker_relative]
        if source == "Authoring":
            ensure_claim_release(workspace, slug)
            claim_path = task_dir(workspace, slug) / "claim.json"
            claim_path.unlink()
            changed.append(relative_git_path(claim_path, workspace))
        move_todo(workspace, slug, source, "Blocked")
        commit_and_push(workspace, control, expected_sha, f"Block task {slug}", changed)
    print(f"BLOCKED\t{slug}")


def unblock(workspace: Path, slug: str, target: str) -> None:
    resolution_relative = f"scratch/{slug}/resolution.md"
    with task_edit_guard(
        workspace, {resolution_relative}
    ) as (control, expected_sha):
        buckets, _ = parse_todo((workspace / "TODO.md").read_text(encoding="utf-8"), workspace)
        if buckets.get(slug) != "Blocked":
            raise CoordinationError(f"task is not Blocked: {slug}")
        if target == "Ready":
            local_manifest(workspace, slug, require_ready=True)
            validate_local_dependencies(workspace, buckets)
        changed = ["TODO.md", *record_resolution(workspace, slug)]
        move_todo(workspace, slug, "Blocked", target)
        commit_and_push(workspace, control, expected_sha, f"Unblock task {slug}", changed)
    print(f"UNBLOCKED\t{slug}\t{target}")


def cancel_task(workspace: Path, slug: str) -> None:
    relative = f"scratch/{slug}/cancellation.md"
    cancellation = workspace / relative
    with coordination_lock(workspace):
        buckets, _, claims = local_state(workspace)
        source = buckets.get(slug)
        if source == "Authoring":
            matches = [claim for claim in claims if claim["slug"] == slug]
            if len(matches) != 1 or matches[0]["owner"] != owner(workspace):
                raise CoordinationError("task does not have one owned claim")
            ensure_claim_release(workspace, slug)
        ignore_local_path(workspace, relative)
        ensure_clean(workspace)
        intent = cancellation_intent_path(workspace, slug)
        control = resolve_default_branch(workspace)
        remote = fetch_ref(workspace, control.ref)
        synchronize_checkout(workspace, control, remote)
        buckets, _ = parse_todo(
            (workspace / "TODO.md").read_text(encoding="utf-8"), workspace
        )
        source = buckets.get(slug)
        if source is None:
            raise CoordinationError(f"task is missing from TODO: {slug}")
        if source in {"Done", "Cancelled"}:
            if source == "Cancelled":
                try:
                    fragment = nonempty_text(cancellation, "cancellation")
                    validate_journal_fragment(fragment, "cancellation")
                except CoordinationError:
                    pass
                else:
                    digest_value = hashlib.sha256(fragment.encode()).hexdigest()
                    if cancellation_was_published(
                        workspace, intent, digest_value, remote
                    ):
                        cancellation.unlink()
                        intent.unlink(missing_ok=True)
                        print(f"CANCELLED\t{slug}")
                        return
            raise CoordinationError(f"terminal task cannot be cancelled: {slug}")
        fragment = nonempty_text(cancellation, "cancellation")
        validate_journal_fragment(fragment, "cancellation")
        digest_value = hashlib.sha256(fragment.encode()).hexdigest()
        for _ in range(10):
            remote = fetch_ref(workspace, control.ref)
            if cancellation_was_published(workspace, intent, digest_value, remote):
                synchronize_checkout(workspace, control, remote)
                cancellation.unlink()
                intent.unlink(missing_ok=True)
                print(f"CANCELLED\t{slug}")
                return
            synchronize_checkout(workspace, control, remote)
            buckets, _ = parse_todo(
                (workspace / "TODO.md").read_text(encoding="utf-8"), workspace
            )
            source = buckets.get(slug)
            if source is None:
                raise CoordinationError(f"task is missing from TODO: {slug}")
            if source in {"Done", "Cancelled"}:
                raise CoordinationError(f"terminal task cannot be cancelled: {slug}")
            with tempfile.TemporaryDirectory(prefix="task-cancel-") as directory:
                temporary = Path(directory)
                git(workspace, "worktree", "add", "--detach", str(temporary), remote)
                try:
                    note = temporary / relative
                    note.parent.mkdir(parents=True, exist_ok=True)
                    note.write_text(f"{fragment.rstrip()}\n", encoding="utf-8")
                    apply_cancellation(temporary, slug, source)
                    # ponytail: the detached worktree starts clean and contains only
                    # this transition, so staging it whole avoids untracked inputs
                    # that are consumed before Git has ever tracked their paths.
                    git(temporary, "add", "--all")
                    git(temporary, "commit", "-m", f"Cancel task {slug}")
                    generated = git(temporary, "rev-parse", "HEAD").stdout.strip()
                    intent.parent.mkdir(parents=True, exist_ok=True)
                    write_json(intent, {"digest": digest_value, "commit": generated})
                    pushed = push_control_ref(temporary, control, remote)
                finally:
                    git(
                        workspace, "worktree", "remove", "--force", str(temporary),
                        check=False,
                    )
            observed = fetch_ref(workspace, control.ref)
            if git(
                workspace, "merge-base", "--is-ancestor", generated, observed,
                check=False,
            ).returncode == 0:
                synchronize_checkout(workspace, control, observed)
                cancellation.unlink()
                intent.unlink(missing_ok=True)
                print(f"CANCELLED\t{slug}")
                return
            if pushed.returncode == 0:
                raise PublicationError("published cancellation is missing from remote state")
            if observed == remote:
                detail = pushed.stderr.strip() or pushed.stdout.strip()
                raise PublicationError(f"cancellation push failed: {detail}")
            synchronize_checkout(workspace, control, observed)
        raise PublicationError("cancellation did not converge after concurrent updates")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("create")
    command.add_argument("slug")
    command.add_argument("--workspace", default=".")
    command = subparsers.add_parser("follow-up")
    command.add_argument("source")
    command.add_argument("slug")
    command.add_argument("--workspace", default=".")
    command = subparsers.add_parser("check")
    command.add_argument("slug")
    command.add_argument("--workspace", default=".")
    command = subparsers.add_parser("ready")
    command.add_argument("slug")
    command.add_argument("--workspace", default=".")
    command = subparsers.add_parser("claim")
    command.add_argument("--workspace", default=".")
    command.add_argument("--slug", help="manual explicit target; automation omits this")
    command = subparsers.add_parser("status")
    command.add_argument("--workspace", default=".")
    command.add_argument("--slug", required=True)
    command = subparsers.add_parser("submit")
    command.add_argument("--workspace", default=".")
    command.add_argument("--slug", required=True)
    command.add_argument("--repository", action="append", default=[])
    command = subparsers.add_parser("complete")
    command.add_argument("--workspace", default=".")
    command.add_argument("--slug", required=True)
    command = subparsers.add_parser("checkpoint")
    command.add_argument("slug")
    command.add_argument("--workspace", default=".")
    command.add_argument("--resume-after")
    command = subparsers.add_parser("block")
    command.add_argument("slug")
    command.add_argument("--workspace", default=".")
    command = subparsers.add_parser("unblock")
    command.add_argument("slug")
    command.add_argument("--workspace", default=".")
    command.add_argument("--to", choices=("Backlog", "Ready"), required=True)
    command = subparsers.add_parser("cancel")
    command.add_argument("slug")
    command.add_argument("--workspace", default=".")
    command = subparsers.add_parser("reorder")
    command.add_argument("slug")
    command.add_argument("--workspace", default=".")
    order = command.add_mutually_exclusive_group(required=True)
    order.add_argument("--before")
    order.add_argument("--after")
    command = subparsers.add_parser("dependency")
    command.add_argument("--workspace", default=".")
    dependency_commands = command.add_subparsers(dest="dependency_command", required=True)
    for name in ("add", "remove"):
        child = dependency_commands.add_parser(name)
        child.add_argument("task")
        child.add_argument("dependency")
        child.add_argument("--workspace", default=argparse.SUPPRESS)
    child = dependency_commands.add_parser("clear")
    child.add_argument("task")
    child.add_argument("--workspace", default=argparse.SUPPRESS)
    command = subparsers.add_parser("record-validation")
    command.add_argument("--workspace", default=".")
    command.add_argument("--slug", required=True)
    command.add_argument("--verdict", choices=("pass", "regression", "unavailable"))
    command.add_argument("--task-plan-records", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    try:
        if args.command == "create":
            create(workspace, args.slug)
        elif args.command == "follow-up":
            create_follow_up(workspace, args.source, args.slug)
        elif args.command == "check":
            check(workspace, args.slug)
        elif args.command == "ready":
            ready(workspace, args.slug)
        elif args.command == "claim":
            claim(workspace, args.slug)
        elif args.command == "status":
            status(workspace, args.slug)
        elif args.command == "submit":
            submit(workspace, args.slug, args.repository)
        elif args.command == "record-validation":
            record_validation(
                workspace, args.slug, args.verdict, args.task_plan_records,
            )
        elif args.command == "complete":
            complete(workspace, args.slug)
        elif args.command == "checkpoint":
            checkpoint(workspace, args.slug, args.resume_after)
        elif args.command == "block":
            block(workspace, args.slug)
        elif args.command == "unblock":
            unblock(workspace, args.slug, args.to)
        elif args.command == "reorder":
            reorder(workspace, args.slug, args.before or args.after, args.before is not None)
        elif args.command == "dependency":
            dependency(
                workspace, args.dependency_command, args.task,
                getattr(args, "dependency", None),
            )
        else:
            cancel_task(workspace, args.slug)
    except CoordinationError as exc:
        print(f"ERROR\t{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
