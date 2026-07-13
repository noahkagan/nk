from __future__ import annotations

from pathlib import Path

import pytest

from nk.task import (
    CoordinationError,
    QUEUE_ORDER,
    move_todo,
    parse_todo,
    validate_manifest,
    validate_new_slug,
)


def todo(entries: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for queue in QUEUE_ORDER:
        lines.extend([f"## {queue}", ""])
        lines.extend(
            f"- [`{slug}`](scratch/{slug}/README.md)"
            for slug, location in entries if location == queue
        )
        lines.append("")
    return "\n".join(lines)


def task(root: Path, slug: str) -> None:
    path = root / "scratch" / slug
    path.mkdir(parents=True)
    (path / "README.md").write_text(f"# {slug}\n")


def test_parser_requires_exact_operational_queue_order(tmp_path: Path) -> None:
    slug = "2026-07-04-test-task"
    task(tmp_path, slug)
    text = todo([(slug, "Ready")])
    parse_todo(text, tmp_path)

    with pytest.raises(CoordinationError, match="exactly once"):
        parse_todo(text.replace("## Blocked", "## Waiting"), tmp_path)


def test_parser_rejects_missing_readme(tmp_path: Path) -> None:
    with pytest.raises(CoordinationError, match="README is missing"):
        parse_todo(todo([("2026-07-04-missing", "Backlog")]), tmp_path)


def test_queue_transitions_append_to_ready(tmp_path: Path) -> None:
    first = "2026-07-04-first"
    repaired = "2026-07-04-repaired"
    task(tmp_path, first)
    task(tmp_path, repaired)
    (tmp_path / "TODO.md").write_text(todo([(first, "Ready"), (repaired, "Blocked")]))

    move_todo(tmp_path, repaired, "Blocked", "Ready")

    ready = (tmp_path / "TODO.md").read_text().split("## Ready", 1)[1].split("## Done", 1)[0]
    assert ready.index(first) < ready.index(repaired)


def test_manifest_distinguishes_unresolved_from_unconstrained() -> None:
    unresolved = {"dependencies": None, "capabilities": None, "resources": None}
    validate_manifest(unresolved, "task")
    with pytest.raises(CoordinationError, match="unresolved"):
        validate_manifest(unresolved, "task", require_ready=True)
    assert validate_manifest(
        {"dependencies": [], "capabilities": {}, "resources": {}},
        "task",
        require_ready=True,
    )["resources"] == {}


@pytest.mark.parametrize(
    "slug",
    ["2026-07-04-description", "2026-07-04-gh-12-description", "2026-07-04-ABC-12-description"],
)
def test_new_slug_accepts_structural_conventions(slug: str) -> None:
    validate_new_slug(slug)


@pytest.mark.parametrize("slug", ["2026-02-30-description", "description", "2026-07-04-UPPER"])
def test_new_slug_rejects_invalid_structure(slug: str) -> None:
    with pytest.raises(CoordinationError):
        validate_new_slug(slug)
