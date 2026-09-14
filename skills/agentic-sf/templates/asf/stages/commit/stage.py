"""commit — land one earlier stage's work product, in its author's own words.

A code stage. `of:` names the stage whose work product this lands, AS IT
STANDS NOW: `commit: {of: plan}` puts the spec on record before code exists,
`commit: {of: implement}` after a verify lands the build the fix loop amended, in
the fix's words, not the envelope the build stage wrote before the checks ran.
Each agent's `commit_message` describes its own work, and no agent's sentence
is reused for another's diff.
A session whose branch is already published pushes too, so an open pull
request shows what the session actually contains.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from engine import git_helper, integration
from engine.data_types import PhaseParams

NAME = "commit"
KIND = "code"
OUTPUT = None                        # lands work; hands the previous envelope on
NEEDS = ()
TASKS = {}


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")

    of: str
    # A clean tree is normally a failure: the stage before claimed to change
    # files and did not. `allow_clean: true` is for the one chain where it is
    # an answer — a builder handed REVIEW REQUESTS may read a thread, judge the
    # ask wrong, and change nothing on purpose (`pr-review`); the run then
    # reports "declined" instead of dying before it can say why.
    allow_clean: bool = False


def check(opts: Options, earlier: dict) -> list[str]:
    output = earlier.get(opts.of)
    if opts.of not in earlier:
        return [f"of: {opts.of!r} is not a stage before this one "
                f"({', '.join(earlier) or 'none'})"]
    if output is None or "commit_message" not in output.model_fields:
        return [f"of: {opts.of!r} hands on {output.__name__ if output else 'nothing'}, "
                f"which carries no commit_message"]
    return []


def run(ctx, opts: Options):
    envelope = ctx.current(opts.of)
    run = ctx.run
    with run.phase(PhaseParams(name=f"commit_{opts.of}", kind="code", owner="git",
                               description=f"Land the {opts.of} on the run's branch, in "
                                           f"the words of the agent that produced it")) as ph:
        message = envelope.commit_message or f"asf({run.adw_id}): {envelope.summary}"
        sha = git_helper.commit_all(run.repo_root, message,
                                    allow_clean=run.resuming or opts.allow_clean)
        if not sha and not run.resuming:
            ph.log(committed="nothing — the tree is clean, on purpose (allow_clean)",
                   reason=envelope.summary)
            return None
        synced = integration.keep_published(run)
        ph.log(sha=sha or "unchanged — this session already committed it",
               message=message, pushed=synced.pushed, notes=" · ".join(synced.notes))
    return None
