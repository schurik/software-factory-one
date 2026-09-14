"""Low-level git operations for code phases. All low-level logic lives in engine.

Every command names the tree it runs in. `cwd` is the FIRST parameter of every
function that measures or mutates a checkout, and it has no default on purpose:
with a worktree per run there are always at least two trees on disk — the
engineer's main checkout and the run's worktree — and a git command that
inherited the ADW process's working directory would silently pick whichever one
the run happened to be launched from. That is how a run in a worktree commits
into the main checkout. Naming the tree makes that class of bug unwritable.

Only the discovery helpers (`is_repo`, `repo_root`, `main_root`) default to the
process cwd, because "which tree am I in" is the one question that cannot
already know its own answer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

Pathish = str | Path


def _git(cwd: Pathish, *args: str) -> str:
    """Run a git command in `cwd` and return its stdout. Raises on failure."""
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _ask_git(cwd: Pathish, *args: str) -> subprocess.CompletedProcess:
    """Run a git command that is a QUESTION — never raises, the caller reads rc."""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


# ── discovery (the only functions whose cwd may be implicit) ─────────────────

def is_repo(cwd: Pathish | None = None) -> bool:
    return _ask_git(cwd or Path.cwd(), "rev-parse", "--git-dir").returncode == 0


def repo_root(cwd: Pathish | None = None) -> Path:
    """Absolute root of the tree at `cwd` — where agents are spawned to work.

    The git toplevel when there is one, else the given directory (ADWs run fine
    in a non-git dir; only a commit phase requires a repo). Inside a worktree
    this is the WORKTREE's directory, which is exactly what a run wants: its
    agents, its gates and its permission snapshots all measure that tree.
    """
    here = Path(cwd or Path.cwd())
    if is_repo(here):
        return Path(_git(here, "rev-parse", "--show-toplevel")).resolve()
    return here.resolve()


def main_root(cwd: Pathish | None = None) -> Path:
    """Absolute root of the PRIMARY checkout, even when called from a worktree.

    The run's record — the trace db, the session dir, context_handoff/ — lives
    in one place per repository, not one place per run, so it is anchored here
    rather than at `repo_root`. `--git-common-dir` is the shared `.git` every
    worktree of a repository points at; its parent is the checkout that owns it.
    """
    here = Path(cwd or Path.cwd())
    if not is_repo(here):
        return here.resolve()
    common = Path(_git(here, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    root = common.parent if common.name == ".git" else repo_root(here)
    return root.resolve()


# ── branches and commits ────────────────────────────────────────────────────

def current_branch(cwd: Pathish) -> str:
    return _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def create_branch(cwd: Pathish, name: str) -> str:
    _git(cwd, "checkout", "-b", name)
    return name


def branch_exists(cwd: Pathish, name: str) -> bool:
    """True when a LOCAL branch of that name exists. Never raises."""
    return _ask_git(cwd, "show-ref", "--verify", "--quiet",
                    f"refs/heads/{name}").returncode == 0


def commit_all(cwd: Pathish, message: str, allow_clean: bool = False) -> str:
    """Stage the working tree and commit it. Returns the new short sha.

    A clean tree is normally a failure: the phase before this one claimed to
    change files and did not, and committing nothing would hide it.

    `allow_clean` is for the one case where a clean tree is the expected answer
    — a RESUMED run reaching a commit phase whose commit the first run already
    made. There is nothing left to stage because the work is already on the
    branch, so the empty string comes back and the caller says so, rather than
    the run dying on a success it made itself.
    """
    if not is_repo(cwd):
        raise RuntimeError(
            "not a git repository — a commit phase needs one. Run `git init` in the "
            "repo root (and make a first commit) before running an ADW that commits.")
    _git(cwd, "add", "-A")
    if not _git(cwd, "status", "--porcelain"):
        if allow_clean:
            return ""
        raise RuntimeError("nothing to commit — the preceding phases changed no files")
    _git(cwd, "commit", "-m", message)
    return _git(cwd, "rev-parse", "--short", "HEAD")


def changed_files(cwd: Pathish) -> list[str]:
    out = _git(cwd, "status", "--porcelain")
    return [line[3:] for line in out.splitlines() if line]


# ── diff plumbing (composed into a ChangeSet by changes.py) ─────────────────

def ref_exists(cwd: Pathish, ref: str) -> bool:
    """True when `ref` resolves to a commit. Never raises — this is a question."""
    return _ask_git(cwd, "rev-parse", "--verify", "--quiet",
                    f"{ref}^{{commit}}").returncode == 0


def rev(cwd: Pathish, ref: str = "HEAD") -> str:
    return _git(cwd, "rev-parse", ref)


def short_sha(cwd: Pathish, ref: str = "HEAD") -> str:
    return _git(cwd, "rev-parse", "--short", ref)


def merge_base(cwd: Pathish, ref: str, other: str = "HEAD") -> str:
    """The commit where `ref` and `other` diverged — the honest base of a branch.

    On the base branch itself this returns HEAD, which makes the diff exactly
    "what is not committed yet". Off it, the diff is the whole branch plus the
    working tree. One command covers both cases, so no ADW has to branch on it —
    including a run on its own `asf/<adw_id>` branch, where it returns the
    commit the worktree was cut from.
    """
    return _git(cwd, "merge-base", ref, other)


def first_subject(cwd: Pathish, base: str, ref: str = "HEAD") -> str:
    """Subject of the first commit unique to `ref` since it diverged from `base`.

    A QUESTION, not a command: empty string on any failure (unknown ref, no
    commits) rather than raising. A caller using this as a fallback title has
    better ways to report "nothing to fall back to" than an exception from
    three functions away.
    """
    result = _ask_git(cwd, "log", "--reverse", "--format=%s", f"{base}..{ref}")
    if result.returncode != 0:
        return ""
    lines = [line for line in result.stdout.splitlines() if line]
    return lines[0] if lines else ""


def is_dirty(cwd: Pathish) -> bool:
    return bool(_git(cwd, "status", "--porcelain"))


def untracked_files(cwd: Pathish) -> list[str]:
    out = _git(cwd, "ls-files", "--others", "--exclude-standard")
    return [line for line in out.splitlines() if line]


def diff_files(cwd: Pathish, base: str) -> list[str]:
    """Tracked files that differ between `base` and the working tree."""
    out = _git(cwd, "diff", "--name-only", base)
    return [line for line in out.splitlines() if line]


def diff_stat(cwd: Pathish, base: str) -> str:
    return _git(cwd, "diff", "--stat", base)


def diff_counts(cwd: Pathish, base: str) -> tuple[int, int]:
    """(insertions, deletions) across the diff. Binary files count as neither."""
    insertions = deletions = 0
    for line in _git(cwd, "diff", "--numstat", base).splitlines():
        added, removed, *_ = line.split("\t")
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return insertions, deletions


def diff_text(cwd: Pathish, base: str) -> str:
    return _git(cwd, "diff", base)


# ── worktrees (one per run — see engine/worktree.py) ────────────────────

def worktree_add(cwd: Pathish, path: Pathish, branch: str,
                 base_ref: str | None = None) -> str:
    """Attach a worktree at `path`. `base_ref` given = create `branch` from it.

    Omit `base_ref` to check out a branch that already exists — which is how a
    joined run, or a rerun after its worktree was pruned, gets its state back:
    the branch is the record, the worktree is only a checkout of it.
    """
    args = ["worktree", "add", str(path)]
    args += ["-b", branch, base_ref] if base_ref is not None else [branch]
    return _git(cwd, *args)


def worktree_list(cwd: Pathish) -> list[dict[str, str]]:
    """Every worktree of this repository: {path, head, branch, detached, prunable}."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in _ask_git(cwd, "worktree", "list", "--porcelain").stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "head": "", "branch": "", "detached": "",
                       "prunable": ""}
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key in ("HEAD", "detached", "prunable"):
            current["head" if key == "HEAD" else key] = value or "yes"
    if current:
        entries.append(current)
    return entries


def worktree_remove(cwd: Pathish, path: Pathish, force: bool = False) -> None:
    """Detach and delete a worktree. Without `force`, git refuses a dirty one."""
    _git(cwd, "worktree", "remove", *(["--force"] if force else []), str(path))


def worktree_prune(cwd: Pathish) -> str:
    """Forget worktrees whose directory is gone — administrative, never destructive."""
    return _git(cwd, "worktree", "prune", "-v")


# ── integration (landing a run's branch — see engine/integration.py) ────

def merge(cwd: Pathish, branch: str, flags: list[str], message: str = "") -> str:
    """Merge `branch` into the branch checked out at `cwd`. Raises on conflict."""
    args = ["merge", *flags]
    if message:
        args += ["-m", message]
    return _git(cwd, *args, branch)


def merge_abort(cwd: Pathish) -> None:
    """Undo a merge that stopped on a conflict. A question, so it never raises."""
    _ask_git(cwd, "merge", "--abort")


def update_branch(cwd: Pathish, source: str, target: str) -> subprocess.CompletedProcess:
    """Fast-forward local branch `target` to `source` without touching a tree.

    `git fetch . <src>:<dst>` moves a ref, so the base branch can be advanced
    while the engineer keeps working — but git refuses when `target` is checked
    out somewhere, which is precisely the case that needs a real merge instead.
    Returns the completed process so the caller can read the refusal.
    """
    return _ask_git(cwd, "fetch", ".", f"{source}:{target}")


def has_remote(cwd: Pathish, remote: str) -> bool:
    return remote in _ask_git(cwd, "remote").stdout.split()


def push(cwd: Pathish, remote: str, branch: str,
         set_upstream: bool = True) -> subprocess.CompletedProcess:
    """Push a branch. Returns the completed process — a rejected push is data."""
    args = ["push"] + (["-u"] if set_upstream else []) + [remote, branch]
    return _ask_git(cwd, *args)


def remote_tip(cwd: Pathish, remote: str, branch: str) -> str:
    """The sha the REMOTE-TRACKING ref holds for `branch`, "" when there is none.

    Read from `refs/remotes/<remote>/<branch>`, which is local: `push` writes it,
    so its existence is the answer to "has this branch ever been published from
    here" and its value is the answer to "does the remote already have this
    commit" — both without a network round trip in a phase that may have nothing
    to do. It can be stale if someone else pushed to the branch, and the push
    that reads it is the thing that finds out; a rejected push is data.
    """
    ref = f"refs/remotes/{remote}/{branch}"
    completed = _ask_git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return completed.stdout.strip() if completed.returncode == 0 else ""
