"""Fixtures for agentic-sf's own tests.

Two things every test here needs and nothing else provides:

  * `engine` on the path. The modules under test are TEMPLATES — stamped into a
    target repo as `asf/engine/` — so they are not an installed package.
    `templates/asf` is what they are stamped FROM, and importing from there is
    importing exactly what a stamped repo runs.
  * A real git repository, stamped the way `install.py` stamps it. `session.
    ensure` cuts a worktree, `permissions.py` reads `git diff`, and the loader
    resolves stages, agents and workflows relative to `asf/factory.yaml` — so
    the tests run against a real install into a tmp_path, not against a mock
    of one.

Helpers live in `asf_helpers.py` and this directory is a package (see
`__init__.py`): another test directory in this repository is not one and imports its own
`conftest` by that bare name, so this one must not be importable as it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = TESTS_DIR.parent
TEMPLATES = SKILL_ROOT / "templates" / "asf"

if str(TEMPLATES) not in sys.path:
    sys.path.insert(0, str(TEMPLATES))

from .asf_helpers import git, install  # noqa: E402


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one commit and a pyproject that names pytest — the
    least a worktree can branch from, and enough for the installer to detect a
    test command."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "asf tests")
    git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("# fixture\n")
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['.']\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def stamped(repo: Path) -> Path:
    """The fixture repo with the factory installed, on the claude_code harness."""
    result = install(repo, "--harness", "claude_code")
    assert result.returncode == 0, result.stdout + result.stderr
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "stamp the factory")
    return repo
