"""The installer, as a subprocess, into a fresh repository."""

from __future__ import annotations

import shutil
from pathlib import Path

from .asf_helpers import SKILL_ROOT, asf, git, install

STAMPED = ["asf/asf.py", "asf/factory.yaml", "asf/engine/session.py", "asf/engine/workflow.py",
           "asf/stages/plan/stage.py", "asf/stages/plan/task.md", "asf/stages/verify/fix.md",
           "asf/agents/planner/agent.md", "asf/engine/frontmatter.py",
           "asf/workflows/sdlc/workflow.yaml", "asf/workflows/ship/workflow.yaml",
           "asf/stages/review/revise.md", "asf/agents/reviewer/agent.md",
           "asf/stages/scout/stage.py", "asf/stages/scout/task.md", "asf/agents/scout/agent.md",
           "asf/workflows/issue/workflow.yaml", "asf/workflows/pr-review/tasks/implement.md",
           "asf/engine/inputs.py", "asf/engine/watch.py", "asf/engine/supervise.py",
           ".env.sample", ".env", "justfile"]


def test_a_fresh_repo_is_stamped_and_its_workflows_check(repo: Path):
    result = install(repo, "--harness", "claude_code")
    assert result.returncode == 0, result.stdout + result.stderr
    for relative in STAMPED:
        assert (repo / relative).is_file(), relative
    assert "harness: claude_code" in (repo / "asf" / "factory.yaml").read_text()
    assert "ASF_SKILL=" in (repo / ".env").read_text()
    # Detected from the fixture's pyproject, written into the stamped quality.py.
    assert '"pytest"' in (repo / "asf" / "engine" / "quality.py").read_text()

    listed = asf(repo, "list")
    assert listed.returncode == 0 and "sdlc" in listed.stdout and "quick" in listed.stdout
    checked = asf(repo, "check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "✓ sdlc: plan -> implement -> verify -> commit" in checked.stdout
    assert "✓ pr-review: implement -> verify -> commit" in checked.stdout


def test_the_runtime_and_the_worktrees_are_gitignored(repo: Path):
    install(repo, "--harness", "pi")
    ignored = (repo / ".gitignore").read_text().splitlines()
    for entry in ("asf/data/", ".asf-worktrees/", ".env"):
        assert entry in ignored
    git(repo, "add", "-A")
    staged = git(repo, "diff", "--cached", "--name-only").splitlines()
    assert not [p for p in staged if p.startswith("asf/data/") or p == ".env"]


def test_a_vendored_skill_keeps_its_visualizer_s_node_modules_out_of_the_host(repo: Path):
    """`up` runs `bun install` inside the SKILL, and a repo that VENDORS the
    skill (`.agents/skills/…`) has that tree in its own working copy.

    A run on a worktree never sees it — a fresh checkout does not carry main's
    untracked files. Under `worktree.enabled: false` it does: the commit stage
    stages `run.repo_root`, which is then the main checkout, and `git add -A`
    sweeps up every package. One flag is not an invariant, and the rule ships
    with the directory, so it holds wherever the skill is checked out and
    whether or not install.py ever touched the host .gitignore.
    """
    install(repo, "--harness", "claude_code")
    vendored = repo / ".agents" / "skills" / "agentic-sf" / "apps" / "visualizer"
    vendored.parent.mkdir(parents=True)
    shutil.copytree(SKILL_ROOT / "apps" / "visualizer", vendored)
    (vendored / "node_modules" / "vue").mkdir(parents=True)
    (vendored / "node_modules" / "vue" / "package.json").write_text("{}\n")

    git(repo, "add", "-A")
    staged = git(repo, "diff", "--cached", "--name-only").splitlines()
    assert not [path for path in staged if "node_modules" in path]
    assert f"{vendored.relative_to(repo).as_posix()}/.gitignore" in staged   # and it travels


def test_a_second_install_skips_and_force_keeps_the_operator_s_config(repo: Path):
    install(repo, "--harness", "claude_code")
    again = install(repo, "--harness", "claude_code")
    assert "skipped" in again.stdout and "stamped: 0 file" in again.stdout

    config = repo / "asf" / "factory.yaml"
    config.write_text(config.read_text() + "\n# mine\n")
    engine = repo / "asf" / "engine" / "utils.py"
    engine.write_text("# clobbered\n")
    forced = install(repo, "--harness", "claude_code", "--force")
    assert forced.returncode == 0
    assert "# clobbered" not in engine.read_text()          # skill code is replaced
    assert config.read_text().endswith("# mine\n")          # the operator's file is not
    assert (repo / "asf" / "factory.yaml.new").is_file()    # the fresh render sits beside it


def test_an_unknown_or_unstampable_harness_is_refused(repo: Path):
    assert "unknown harness 'codex'" in install(repo, "--harness", "codex").stderr
    assert "unknown harness 'fake'" in install(repo, "--harness", "fake").stderr
    assert "pass --harness" in install(repo).stderr          # no terminal to ask on


def test_a_foreign_justfile_is_left_alone_and_ours_lands_beside_it(repo: Path):
    (repo / "justfile").write_text("# my own recipes\ndefault:\n    @just --list\n")
    result = install(repo, "--harness", "claude_code")
    assert result.returncode == 0
    assert (repo / "justfile").read_text().startswith("# my own recipes")
    assert (repo / "asf.justfile").read_text().startswith("# agentic-sf recipes.")
    assert "just -f asf.justfile" in result.stdout
