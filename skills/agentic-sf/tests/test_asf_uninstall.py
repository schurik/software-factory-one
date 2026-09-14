"""The uninstaller, as a subprocess, against stamped repos in every state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .asf_helpers import SKILL_ROOT, git, install

UNINSTALL = SKILL_ROOT / "scripts" / "uninstall.py"


def uninstall(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(UNINSTALL), *args], cwd=cwd,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_a_stamped_repo_is_emptied_and_the_repo_s_own_files_are_kept(repo: Path):
    install(repo, "--harness", "claude_code")
    (repo / ".env").write_text((repo / ".env").read_text() + "ANTHROPIC_API_KEY=sk-mine\n")
    (repo / "asf" / "workflows" / "mine").mkdir()
    (repo / "asf" / "workflows" / "mine" / "workflow.yaml").write_text("name: mine\n")
    db = repo / "asf" / "data" / "asf.db"
    db.parent.mkdir(parents=True)
    db.write_text("")

    dry = uninstall(repo, "--dry-run")
    assert dry.returncode == 0 and "nothing was deleted" in dry.stdout
    assert "workflows/mine/workflow.yaml" in dry.stdout      # your own work, named first
    assert (repo / "asf").is_dir()

    asked = uninstall(repo)
    assert asked.returncode == 1 and "pass --yes" in asked.stderr

    done = uninstall(repo, "--yes")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (repo / "asf").exists() and not (repo / "justfile").exists()
    assert not (repo / ".env.sample").exists()
    assert not db.exists()                                    # the record goes with asf/
    env = (repo / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-mine" in env and "ASF_SKILL=" not in env
    assert not (repo / ".gitignore").exists()      # nothing but the block was in it
    assert ".gitignore (nothing left in it)" in done.stdout
    assert "left for you" in done.stdout and ".env" in done.stdout
    assert uninstall(repo, "--yes").stdout.startswith("no factory here")


def test_a_db_pointed_outside_asf_is_deleted_by_name(repo: Path):
    install(repo, "--harness", "pi")
    config = repo / "asf" / "factory.yaml"
    config.write_text(config.read_text().replace("db: asf/data/asf.db", "db: .trace/asf.db"))
    db = repo / ".trace" / "asf.db"
    db.parent.mkdir()
    db.write_text("")
    db.with_name("asf.db-wal").write_text("")
    done = uninstall(repo, "--yes")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not db.exists() and not db.with_name("asf.db-wal").exists()
    assert not (repo / ".trace").exists() and not (repo / "asf").exists()


def test_run_worktrees_go_and_branches_stay_unless_asked(repo: Path):
    install(repo, "--harness", "claude_code")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "stamp")
    tree = repo / ".asf-worktrees" / "a1b2c3d4"
    git(repo, "worktree", "add", "-q", "-b", "asf/a1b2c3d4", str(tree))
    done = uninstall(repo, "--yes")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not tree.exists() and "asf/a1b2c3d4" in git(repo, "branch", "--list", "asf/*")
    assert "still here" in done.stdout
    assert "asf/a1b2c3d4" not in git(repo, "worktree", "list")

    install(repo, "--harness", "claude_code")
    gone = uninstall(repo, "--yes", "--branches")
    assert gone.returncode == 0 and git(repo, "branch", "--list", "asf/*") == ""


def test_a_live_run_blocks_the_uninstall_unless_forced(repo: Path):
    install(repo, "--harness", "claude_code")
    session = repo / "asf" / "data" / "sessions" / "a1b2c3d4"
    session.mkdir(parents=True)
    (session / "run.json").write_text(json.dumps(
        {"adw_id": "a1b2c3d4", "status": "running", "pid": os.getpid()}))
    blocked = uninstall(repo, "--yes")
    assert blocked.returncode == 1 and "asf kill a1b2c3d4" in blocked.stderr
    assert (repo / "asf").is_dir()
    forced = uninstall(repo, "--yes", "--force")
    assert forced.returncode == 0 and not (repo / "asf").exists()


def test_the_skill_itself_is_never_the_target():
    refused = uninstall(SKILL_ROOT, "--yes")
    assert refused.returncode == 1 and "refusing" in refused.stderr
