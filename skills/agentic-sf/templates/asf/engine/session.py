"""Session lifecycle: pin-or-create an adw_id, build the run's workspace and Run.

`ensure(cfg, adw_id)` joins the session if it exists or creates it under
exactly that id (pinned ids for repeatable runs); omitted, a fresh id is
minted and printed so the next ADW can pick it up.

It is also the last moment a run can be refused for free, which is why
`preflight.before_run` is the first thing it does — see that module.

`resume=True` adds one thing to joining: the agent phases this session already
recorded are handed back from its own directory instead of being asked again, so a
chain that died in its last phase restarts AT that phase rather than at the top.
It needs a pinned `adw_id` — there is nothing to resume without one — and
everything code owns still runs for real. See `engine/replay.py`.

This is also where a run stops being able to hurt the engineer's checkout. The
worktree is created here, before anything else exists, because everything
downstream derives its tree from `run.repo_root`: agents are spawned in it, the
permission snapshot fingerprints it before the first call, gates measure it, and
commit phases commit it. Create it later and each of those would need its own
opt-in.

The order below matters. `main_root` is resolved first — from the git common
dir, so it is the engineer's checkout even when an ADW is launched from inside
a worktree — and the trace db and session dir are anchored to it. One db for
every concurrent run, and a record that outlives the worktree it describes.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from . import artifacts, git_helper, preflight, worktree
from .data_types import RunSpec, RunState, FactoryConfig, WorktreeRequest
from .hitl import HitlPolicy
from .runner import Run
from .tracer import Tracer
from .utils import anchor, engineer_name, new_id, now_iso


def _finalize_when_killed(run: Run) -> None:
    """A killed run still closes its own trace, and still keeps its worktree.

    Python's default SIGTERM handling exits without unwinding, so `just kill`
    (or any `kill <pid>`) would leave the session reading `running` forever and
    its process rows open — the trace would claim work is in flight that is
    already dead. Turning the signal into SystemExit both finalizes here and
    lets the phase context manager record the phase as failed on the way out.

    Nothing here touches the worktree. A killed run is exactly the one whose
    tree you want to open afterwards, and `run.finish()` — the only place that
    releases one — is never reached on this path.
    """
    def handler(signum, _frame):
        run.tracer.session_finish(run.adw_id, ok=False)   # also closes process rows
        artifacts.finish_run(run.session_dir, "fail")     # and the session's own record
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def ensure(cfg: FactoryConfig, adw_id: str | None = None, resume: bool = False,
           hitl: str = "", name: str | None = None) -> Run:
    """Pin or create the session and return the Run.

    `name` is what the trace and the UI call this workflow. The runner passes
    the workflow's name; without one the script's own name is used, which is
    what a hand-written script wants.
    """
    workflow = name or Path(sys.argv[0]).stem
    if resume and not adw_id:
        raise SystemExit("--resume needs --adw-id: there is nothing to resume without "
                         "the session that recorded it. `just sessions` lists them.")
    adw_id = adw_id or new_id(8)
    main_root = git_helper.main_root()          # the engineer's checkout, always
    # BEFORE the worktree, this session's run.json and its process record exist.
    # A run that cannot write its own directory, or whose base_ref does not
    # resolve, dies a few seconds later having already cut a branch and claimed
    # an id — so it is refused here, while the repo is still untouched. Warnings
    # survive to be said on the console below, where they are read.
    # ...and a `--hitl` nobody can parse is refused in the same breath, for the
    # same reason: it would otherwise die inside Run() with a branch already cut.
    try:
        HitlPolicy(cfg.hitl, hitl or os.environ.get("ASF_HITL", ""))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    warnings = preflight.before_run(cfg, main_root)
    workspace = worktree.ensure(WorktreeRequest(main_root=main_root, adw_id=adw_id,
                                                config=cfg.worktree))
    tracer = Tracer(anchor(main_root, cfg.observability.db),
                    anchor(main_root, f"{cfg.defaults.data_dir}/sessions/{adw_id}/events.jsonl"))
    run = Run(RunSpec(cfg=cfg, adw_id=adw_id, engineer=engineer_name(),
                      workspace=workspace, resume=resume, hitl=hitl), tracer)
    tracer.session_start(adw_id, run.engineer, adw_name=workflow)
    # What the session already knows about itself, from its own directory. An
    # issue-triggered session that a later ADW re-enters must still know it was
    # issue-triggered — integration.py refuses to merge on that — and a session
    # whose branch is already a pull request must know THAT, or it proposes the
    # branch a second time instead of pushing to the PR it has.
    recorded = artifacts.read_run(run.session_dir)
    if recorded:
        run.adopt_provenance(recorded.trigger, recorded.issue_url, recorded.pr_url)
    tracer.session_workspace(adw_id, workspace)
    # This process is the run. Record it before any phase opens, so a run that
    # hangs in its first agent call is still killable by adw_id.
    tracer.process_start(adw_id, "adw", "", os.getpid(),
                         " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]))
    artifacts.record_process(run.session_dir, "adw", "", os.getpid(),
                             " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]))
    # The same fact in the session's OWN directory, and the only place it is
    # written whole: `command` is the argv as a list, so `just resume` can launch
    # this workflow again without a db, without unquoting, and without the 500
    # character clip the process row applies. artifacts.py says why files win.
    artifacts.start_run(run.session_dir, RunState(
        adw_id=adw_id, workflows=[workflow],
        command=[Path(sys.argv[0]).name, *sys.argv[1:]],
        pid=os.getpid(), engineer=run.engineer, status="running",
        started_at=now_iso(), repo_root=str(workspace.repo_root),
        branch=workspace.branch, trigger=run.trigger,
        issue_url=run.issue_url, pr_url=run.pr_url))
    _finalize_when_killed(run)
    run.console.session_started(adw_id, run.engineer)
    run.console.note(_workspace_line(workspace))
    # Two lines, not one: the console clips a note at 160 characters, and a fix
    # that gets cut off is the half worth keeping.
    for finding in warnings:
        run.console.note(f"preflight — {finding.detail}")
        if finding.fix:
            run.console.note(f"fix: {finding.fix}")
    if resume:
        run.console.note(run.replay.summary())
    if run.hitl.override or cfg.hitl.default or any(cfg.hitl.gates.values()):
        run.console.note(run.hitl.summary())
    return run


def _workspace_line(workspace) -> str:
    """The one line that says where this run's work will actually land."""
    if not workspace.enabled:
        return f"workspace: {workspace.repo_root} (no worktree — running in place)"
    verb = "joined" if workspace.joined else "created"
    return (f"workspace: {verb} {workspace.repo_root} on {workspace.branch} "
            f"from {workspace.base_ref} @ {workspace.base_commit[:7]}")
