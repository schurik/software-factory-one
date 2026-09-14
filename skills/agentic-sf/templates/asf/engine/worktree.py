"""A git worktree and a branch per run — isolation, not a sandbox.

Upstream runs in the engineer's working tree, on whatever branch happens to be
checked out. That makes two runs unable to be concurrent, makes a failed run
destructive, and makes `commit_all()` pick up whatever else was lying around.

So every run gets `<worktrees_dir>/<adw_id>` on branch `asf/<adw_id>`, cut from
a base ref pinned once at run start. `Run.repo_root` becomes that directory, and
because agents, gates, quality blocks and the permission snapshot all derive
from `repo_root`, the isolation follows from one assignment rather than from
fourteen call sites remembering to opt in.

Three properties this file exists to keep:

  * **A joined run re-attaches.** A second ADW pinned to the same `--adw-id`
    must land in the SAME worktree, never a second one — the first ADW's
    uncommitted work is the second one's input.
  * **The branch is the record, the worktree is a copy of it.** So a worktree
    may be removed whenever it is clean, and re-created later from its branch
    with nothing lost. That is what makes pruning on success safe.
  * **A failure keeps its evidence.** A failed or killed run's worktree is where
    you go to see what happened, and anything with uncommitted work in it stays
    put whatever the outcome.

It is not a sandbox. An agent with `bash` can `cd` anywhere; `permissions.py`
remains the boundary, and it measures the tree this module hands it.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import artifacts, git_helper
from .data_types import Workspace, WorktreeConfig, WorktreeInfo, WorktreeRequest
from .utils import anchor, ensure_dir, now_iso

META_SUFFIX = ".json"          # <worktrees_dir>/<adw_id>.json, beside the worktree
ENDED = {"success", "fail"}    # session statuses that mean nothing is using the tree


def _meta_path(root: Path, adw_id: str) -> Path:
    """Where a run's pinned base is recorded — BESIDE the worktree, never in it.

    Inside, it would be an untracked file in the tree the run commits with
    `git add -A`, so the factory's own bookkeeping would end up in the
    engineer's history.
    """
    return root / f"{adw_id}{META_SUFFIX}"


def _base_ref_of(main_root: Path, config: WorktreeConfig) -> str:
    """What to cut from: the configured ref, else whatever main has checked out.

    A detached main checkout has no branch name to inherit, so the sha is used —
    it is still a perfectly good thing to branch from, and pinning it is the
    point.
    """
    if config.base_ref:
        return config.base_ref
    branch = git_helper.current_branch(main_root)
    return git_helper.rev(main_root, "HEAD") if branch == "HEAD" else branch


def ensure(request: WorktreeRequest) -> Workspace:
    """The run's workspace: create the worktree, re-attach to it, or do neither.

    Returns a Workspace whose two roots are the same directory when worktrees
    are off, when this is not a git repository, or when the repository has no
    commit to branch from — every one of which is a legitimate way to run an
    ADW, and none of which is worth failing a run over.
    """
    main = Path(request.main_root).resolve()
    config = request.config
    plain = Workspace(main_root=main, repo_root=main)

    if not config.enabled or not git_helper.is_repo(main):
        return plain
    if not git_helper.ref_exists(main, "HEAD"):
        return plain          # a fresh `git init` has nothing to branch from yet

    root = anchor(main, config.dir)
    path = root / request.adw_id
    branch = f"{config.branch_prefix}{request.adw_id}"
    meta = _meta_path(root, request.adw_id)

    if path.is_dir():
        return _reattach(path, main, branch, meta)

    # A worktree whose directory was deleted by hand still holds its
    # registration, and `worktree add` refuses the same path twice. Pruning is
    # administrative — it forgets records for directories that are already gone
    # and touches nothing that exists.
    git_helper.worktree_prune(main)
    ensure_dir(root)

    if git_helper.branch_exists(main, branch):
        # The run's branch outlived its worktree — a pruned success, or a rerun.
        # Check it out again rather than branching a second time from the base:
        # the branch is the record, and re-creating it would discard the record.
        git_helper.worktree_add(main, path, branch)
        recorded = _read_meta(meta)
        base_ref = recorded.get("base_ref") or _base_ref_of(main, config)
        base_commit = recorded.get("base_commit") or git_helper.merge_base(
            path, base_ref, "HEAD")
    else:
        base_ref = _base_ref_of(main, config)
        base_commit = git_helper.rev(main, base_ref)
        git_helper.worktree_add(main, path, branch, base_ref)

    workspace = Workspace(main_root=main, repo_root=path.resolve(), enabled=True,
                          branch=branch, base_ref=base_ref, base_commit=base_commit,
                          created=True)
    _write_meta(meta, request.adw_id, workspace)
    return workspace


def _reattach(path: Path, main: Path, branch: str, meta: Path) -> Workspace:
    """Join the worktree an earlier ADW in this session created.

    The base ref is read back rather than re-derived: it was pinned when the
    session started, and re-deriving it now would measure against whatever the
    engineer's checkout has moved on to since.
    """
    if not git_helper.is_repo(path):
        raise RuntimeError(
            f"{path} exists but is not a git worktree — an earlier run left a "
            f"directory behind, or the path is in use. Move it aside, or pass a "
            f"fresh --adw-id.")
    recorded = _read_meta(meta)
    return Workspace(
        main_root=main,
        repo_root=path.resolve(),
        enabled=True,
        branch=recorded.get("branch") or git_helper.current_branch(path) or branch,
        base_ref=recorded.get("base_ref", ""),
        base_commit=recorded.get("base_commit", ""),
        created=False,
    )


def _read_meta(meta: Path) -> dict:
    try:
        return json.loads(meta.read_text())
    except (OSError, ValueError):
        return {}


def _write_meta(meta: Path, adw_id: str, workspace: Workspace) -> None:
    meta.write_text(json.dumps({
        "adw_id": adw_id,
        "path": str(workspace.repo_root),
        "branch": workspace.branch,
        "base_ref": workspace.base_ref,
        "base_commit": workspace.base_commit,
        "created_at": now_iso(),
    }, indent=2))


# ── lifecycle ────────────────────────────────────────────────────────────────

def release(workspace: Workspace) -> str:
    """Remove a finished run's worktree, but only when it is safe to.

    Safe means clean. A worktree with uncommitted or untracked work in it is the
    only copy of that work — a plan-only chain never commits, so its `plan.md`
    lives exactly there — and removing it to reclaim a few megabytes would be
    the same harm permissions.py exists to prevent. The branch survives either
    way, so a clean worktree is a copy and nothing is lost by dropping it.

    Returns the sentence the console and the trace show. Never raises: cleanup
    failing must not turn an accepted run into a failed one.
    """
    if not workspace.enabled:
        return "no worktree to release"
    try:
        if git_helper.is_dirty(workspace.repo_root):
            return (f"kept {workspace.repo_root} — uncommitted work on "
                    f"{workspace.branch}")
        git_helper.worktree_remove(workspace.main_root, workspace.repo_root)
        return f"removed {workspace.repo_root}; branch {workspace.branch} retained"
    except RuntimeError as error:
        return f"kept {workspace.repo_root} — {error}"


def remove(main_root, path, force: bool = False) -> str:
    """Delete one worktree by path. `force` also discards uncommitted work."""
    git_helper.worktree_remove(main_root, path, force=force)
    return f"removed {path}"


def inventory(main_root, config: WorktreeConfig, sessions_dir: str = "") -> list[WorktreeInfo]:
    """Every run worktree on disk, with the state of the run that owns it.

    "Left behind" and "orphaned" are different things: a killed run keeps its
    worktree on purpose, and git has no idea whether the process that made this
    directory is still alive. The runs' own records say so — one `run.json` per
    session, never the trace db, which may not exist on this machine at all.
    Statuses come from there; git supplies the paths.
    """
    root = anchor(main_root, config.dir)
    statuses = artifacts.statuses(Path(sessions_dir)) if sessions_dir else {}
    found: list[WorktreeInfo] = []
    for entry in git_helper.worktree_list(main_root):
        path = Path(entry["path"])
        branch = entry.get("branch", "")
        mine = branch.startswith(config.branch_prefix) or root in path.parents
        if not mine or path.resolve() == Path(main_root).resolve():
            continue
        adw_id = (branch.removeprefix(config.branch_prefix) if branch.startswith(config.branch_prefix)
                  else path.name)
        prunable = bool(entry.get("prunable"))
        found.append(WorktreeInfo(
            path=str(path), branch=branch, adw_id=adw_id, prunable=prunable,
            dirty=(not prunable and path.is_dir() and git_helper.is_dirty(path)),
            status=statuses.get(adw_id, "unknown"),
        ))
    return found


def reclaimable(info: WorktreeInfo, force: bool = False) -> bool:
    """Whether cleanup may take this worktree. Conservative on purpose.

    A run that has not ended is still using its tree. A tree holding
    uncommitted work is the only copy of that work. `force` overrides only the
    second — nothing reclaims a worktree out from under a live run.
    """
    if info.status not in ENDED:
        return False
    return force or not info.dirty
