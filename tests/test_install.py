from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_install_preserves_clusters_and_runs_outside_source(tmp_path: Path) -> None:
    source = Path(__file__).parents[1]
    home = tmp_path / "home"
    nk_home = tmp_path / "nk"
    cluster = nk_home / "clusters" / "keep" / "config.json"
    cluster.parent.mkdir(parents=True)
    cluster.write_text("preserve me\n")
    old_skill = home / ".agents/skills/task-coordination"
    old_skill.mkdir(parents=True)
    (old_skill / "old.txt").write_text("old installation\n")
    environment = {**os.environ, "HOME": str(home), "NK_HOME": str(nk_home)}

    subprocess.run([str(source / "install.sh")], cwd=tmp_path, env=environment, check=True)
    result = subprocess.run(
        [str(home / ".local/bin/nk"), "--help"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "task" in result.stdout
    assert cluster.read_text() == "preserve me\n"
    assert (home / ".agents/skills/task-coordination").is_symlink()
    assert (home / ".agents/skills/workstream-decoupling").is_symlink()


def test_windows_installer_populates_harness_skill_discovery() -> None:
    script = (Path(__file__).parents[1] / "install.ps1").read_text()

    assert 'Join-Path $HOME ".agents\\skills"' in script
    assert 'Join-Path $HOME ".claude\\skills"' in script
    assert "Copy-Item -Recurse $_.FullName $Destination" in script


def test_installers_record_git_revision_and_dirty_state() -> None:
    root = Path(__file__).parents[1]
    shell = (root / "install.sh").read_text()
    powershell = (root / "install.ps1").read_text()

    for script in (shell, powershell):
        assert "REVISION" in script
        assert "status --porcelain" in script
