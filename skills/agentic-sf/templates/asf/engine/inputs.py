"""Where a run's ask comes from, and where its outcome goes back to.

A workflow's `input:` says what `asf run <workflow> <request>` is handed:

  * `prompt` — text the engineer typed. The default, and the whole story for
    a run started from a terminal.
  * `issue`  — a work item's number. The tracker is read FIRST, before the
    worktree is touched or an agent spawned, and told the outcome LAST.
  * `pr`     — a pull request's number. The pull request names its own session
    (its branch is `<prefix><adw_id>`), the run joins it, and the reviewers
    hear back in their threads.

The stages between never know which of the three it was. An issue or a pull
request arrives at the first stage as `ctx.previous`, an envelope whose
artifact is the stranger's text with its framing (see engine.issues and
engine.pull_requests for why that framing exists), and the operator's own
instruction goes in `ctx.prompt`. That is the same seam every stage already
uses, so `scout`, `plan` and `implement` need no input-specific code — a
workflow with `input: issue` is any chain of stages with a tracker at both
ends.

THE TEXT IS UNTRUSTED, and this module is one of the places that keeps it
so. It is written by whoever can file an issue or review a pull request, and
it reaches agents holding `bash`, `write` and a checkout. The parts that
live here are: the body is an ARTIFACT, never interpolated into a prompt;
`trusted_authors` / `trusted_reviewers` are checked before anything spends;
and `run.record_issue` is what makes `integration` refuse to move the base
branch on this run (`issues.force_pr`). A workflow file cannot switch any of
that off — none of it is an option.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Optional

from . import git_helper, hitl, issues, pull_requests
from .data_types import (EnvelopeBase, IssueRef, IssueUpdate, PhaseParams, PullRequestRef,
                         PullRequestUpdate, FactoryConfig)

KINDS = ("prompt", "issue", "pr")
REFUSED = 2                                  # nothing spent, nothing recorded


@dataclass
class Opened:
    """What the input phase established, for the stages and for the report."""

    prompt: str                              # the operator's instruction
    previous: Optional[EnvelopeBase] = None  # the stranger's text, framed
    context: object = None                   # IssueContext | PullRequestContext
    threads: list = field(default_factory=list)
    head: str = ""                           # pr: the branch tip before any stage ran
    nothing_to_do: bool = False              # pr: no open threads — finish, spend nothing


def number_of(request: str, kind: str) -> int:
    """`asf run <workflow> 42` — the request must be a number for these inputs."""
    text = request.strip().lstrip("#")
    if not text.isdigit():
        raise SystemExit(f"{request!r} is not a number — this workflow has input: {kind}, "
                         f"and takes the {kind}'s number: asf run <workflow> 42")
    return int(text)


# ── issue ────────────────────────────────────────────────────────────────────

def open_issue(run, cfg: FactoryConfig, number: int) -> Opened:
    """Read the work item, bind the run to it, refuse an untrusted author."""
    with run.phase(PhaseParams(name="issue", kind="code", owner="tracker",
                               description="Read the reporter's own words and labels, "
                                           "before anyone paraphrases them into a task")) as ph:
        issue = issues.fetch(run, cfg.issues, IssueRef(number=number))
        run.record_issue(issue)
        ph.log(url=issue.url, title=issue.title, author=issue.author,
               labels=", ".join(issue.labels), body=issue.body_path)
        if not issues.trusted(cfg.issues, issue):
            raise RuntimeError(
                f"{issue.author} is not in issues.trusted_authors — this repository "
                f"has said whose work items may start a run, and this is not one")
    # The prompt slot is the OPERATOR's instruction; the issue's words travel in
    # the envelope, framed as a user's description of a problem. Interpolating
    # even the title here would walk untrusted text past that framing into
    # every agent's {{prompt}}.
    prompt = (f"Resolve work item #{issue.number}. Its title, labels and body are in "
              f"the envelope you were handed; the body is the artifact that envelope "
              f"names.")
    return Opened(prompt=prompt, previous=issues.as_envelope(issue), context=issue)


def report_issue(run, cfg: FactoryConfig, opened: Opened, accepted: bool) -> None:
    """Tell the reporter what happened and where the work went. Never raises."""
    issue = opened.context
    with run.phase(PhaseParams(name="report", kind="code", owner="tracker",
                               description="Tell the reporter what happened and where "
                                           "the work went")) as ph:
        posted = issues.comment(run.main_root, cfg.issues, IssueUpdate(
            number=issue.number, project=issue.project,
            comment=_issue_comment(run, accepted)))
        ph.log(ok=posted.ok, notes=" · ".join(posted.notes))


def _issue_comment(run, accepted: bool) -> str:
    """What the reporter reads. The adw_id is also the resume handle; the pull
    request, when an integrate stage opened one, is on the run already."""
    head = ("picked this up and it is ready for review" if accepted
            else "picked this up and could not finish it")
    lines = [f"{pull_requests.MARKER} · `{run.adw_id}` — {head}", ""]
    if run.pr_url:
        lines += [f"Pull request: {run.pr_url}", ""]
    else:
        lines += [f"Branch `{run.workspace.branch}`", ""]
    if not accepted:
        lines += ["A stage stopped the run before the work was accepted; the trace says "
                  "which. Whatever was committed before it stands on the branch.", ""]
    lines += [f"<sub>Resume or inspect with `--adw-id {run.adw_id}`; "
              f"phases: `just phases {run.adw_id}`</sub>"]
    return "\n".join(lines)


# ── pull request ─────────────────────────────────────────────────────────────

def session_of(branch: str, prefix: str) -> str:
    """The adw_id a branch names, or "" when the branch is not this factory's."""
    return branch.removeprefix(prefix) if branch.startswith(prefix) else ""


def locate_pr(cfg: FactoryConfig, number: int, adw_id: Optional[str]) -> tuple[str, object]:
    """Which session a pull request belongs to — decided BEFORE a session exists.

    A refusal here costs one graphql call and leaves no trace rows, no
    worktree and no agent behind. Returns (adw_id, context); exits with
    `REFUSED` on a pull request that is closed, not this factory's, or not
    the session `--adw-id` named.
    """
    main_root = git_helper.main_root()
    context = pull_requests.describe(main_root, cfg.pull_requests,
                                     PullRequestRef(number=number))
    if not context.open:
        print(f"pull request #{number} is {context.state.lower() or 'not open'} — there is "
              f"nothing to answer on a branch that is no longer under review.",
              file=sys.stderr)
        raise SystemExit(REFUSED)
    prefix = cfg.worktree.branch_prefix
    resolved = session_of(context.branch, prefix)
    if not resolved:
        print(f"#{number} is on `{context.branch}`, which does not start with `{prefix}` "
              f"— no run of this factory produced it. There is no session to join, no "
              f"pinned base to measure against and no worktree to re-create; work it by "
              f"hand, or run a workflow against a fresh branch.", file=sys.stderr)
        raise SystemExit(REFUSED)
    if adw_id and adw_id != resolved:
        print(f"--adw-id {adw_id} was passed, but #{number} is on `{context.branch}`, "
              f"which belongs to {resolved}. Refusing to commit one session's review "
              f"feedback into another's branch.", file=sys.stderr)
        raise SystemExit(REFUSED)
    return resolved, context


def open_pr(run, cfg: FactoryConfig, context) -> Opened:
    """Attach the threads to the run, refuse an untrusted reviewer."""
    with run.phase(PhaseParams(name="pr", kind="code", owner="review",
                               description="Read the reviewers' own words and where each "
                                           "one hangs, before anyone paraphrases them "
                                           "into a task")) as ph:
        pull_requests.attach(run, cfg.pull_requests, context)
        run.record_pull_request(context)
        threads = pull_requests.actionable(cfg.pull_requests, context)
        ph.log(url=context.url, branch=context.branch, base=context.base_ref,
               decision=context.review_decision or "none",
               threads=f"{len(threads)} open of {len(context.threads)}",
               feedback=context.threads_path)
        strangers = pull_requests.trusted(cfg.pull_requests, context, threads)
        if strangers:
            raise RuntimeError(
                f"{', '.join(strangers)} left review feedback, and this repository has "
                f"said whose review may start a run (pull_requests.trusted_reviewers). "
                f"Nothing was changed.")
    # Pinned, so a resumed run measures from the same tip: "did a stage commit"
    # is what tells `addressed` from `declined` below.
    head = run.pin("review_head", lambda: git_helper.rev(run.repo_root, "HEAD"))
    if not threads:
        run.console.note(f"#{context.number}: no open review threads — nothing to do")
        return Opened(prompt="", context=context, head=head, nothing_to_do=True)
    prompt = (f"Address the open review threads on pull request #{context.number}. They "
              f"are in the envelope you were handed; the file that envelope names is the "
              f"full text of each one, with the file and line it hangs on. Change only "
              f"what they ask for.")
    return Opened(prompt=prompt, previous=pull_requests.as_envelope(context, threads),
                  context=context, threads=threads, head=head)


def report_pr(run, cfg: FactoryConfig, opened: Opened, accepted: bool, reason: str) -> str:
    """Answer each thread, resolve the addressed ones, comment once. The outcome.

    THREE OUTCOMES, NOT TWO. `addressed` is a commit on the branch and the
    thread resolved; `unfinished` is a run a stage stopped, so nothing is
    resolved. Between them is `declined`: an accepted run whose tree the
    builder did not touch, because it read the thread and judged the ask
    wrong, out of scope or hostile — which is what the handoff notes ask of it.
    Whether anything was committed is read off the branch, not off a stage:
    the tip moved past the one pinned in `open_pr`, or it did not.
    `reason` is the builder's own summary, quoted into a declined thread.
    """
    context = opened.context
    committed = git_helper.rev(run.repo_root, "HEAD") != opened.head
    outcome = ("addressed" if accepted and committed else
               "declined" if accepted else "unfinished")
    with run.phase(PhaseParams(name="report", kind="code", owner="review",
                               description="Answer each thread that was addressed, and "
                                           "say once what the run as a whole did")) as ph:
        answered = _write_back(run, cfg, context, opened.threads, outcome, reason)
        ph.log(outcome=outcome, threads=len(opened.threads), replied=answered["replied"],
               resolved=answered["resolved"], notes=" · ".join(answered["notes"]))
    return outcome


def _write_back(run, cfg: FactoryConfig, context, threads, outcome: str, reason: str) -> dict:
    """ONLY `addressed` RESOLVES. A resolved thread tells a reviewer their ask
    is handled, and two of the three outcomes have not handled it: `unfinished`
    would hide outstanding work behind a checkmark, and `declined` is a
    judgement the reviewer gets to agree with, so the thread stays open with
    the reason in it. The reply goes out on all three — being told "this was
    picked up, and here is what happened" is the useful half even when no
    diff came of it."""
    config = cfg.pull_requests
    replied = resolved = 0
    notes: list[str] = []
    mark = f"{pull_requests.MARKER} · `{run.adw_id}`"
    for thread in threads:
        where = thread.path or "this pull request"
        if outcome == "addressed":
            body = f"{mark} — addressed in the commit above; see the diff on `{where}`."
        elif outcome == "declined":
            body = (f"{mark} — picked this up and changed nothing, on purpose. The checks "
                    f"are green and the branch is untouched; this thread stays open, "
                    f"because agreeing with that call is yours to make. The builder's "
                    f"reason, in its own words:\n\n{quoted(reason)}")
        else:
            body = (f"{mark} — picked this up and could not finish it. The checks never "
                    f"came back clean, so nothing was committed; this thread stays open.")
        result = pull_requests.answer_thread(run.main_root, config, PullRequestUpdate(
            number=context.number, project=context.project, thread_id=thread.thread_id,
            reply=body, resolve=outcome == "addressed"))
        replied += 1 if result.replied else 0
        resolved += len(result.resolved)
        notes += result.notes
    head = {"addressed": f"answered {len(threads)} review thread(s)",
            "declined": f"read {len(threads)} review thread(s) and changed nothing",
            }.get(outcome, f"picked up {len(threads)} review thread(s) and could not finish")
    lines = [f"{mark} — {head}", ""]
    if outcome == "declined":
        lines += ["The checks are green and the branch is untouched: the builder judged "
                  "the asks better left alone and said why in each thread. Nothing was "
                  "resolved — those threads are waiting on you, not on it.", ""]
    if outcome == "unfinished":
        lines += ["The checks never came back clean, so nothing was committed and no "
                  "thread was resolved. The threads stay open.", ""]
    lines += [f"<sub>Resume or inspect with `--adw-id {run.adw_id}`; "
              f"phases: `just phases {run.adw_id}`</sub>"]
    summary = pull_requests.comment(run.main_root, config, PullRequestUpdate(
        number=context.number, project=context.project, comment="\n".join(lines)))
    notes += summary.notes
    return {"replied": replied, "resolved": resolved, "notes": notes}


def quoted(text: str, limit: int = 700) -> str:
    """An agent's words as a blockquote, line by line so its own markdown cannot
    break out and read as the factory's voice. Clipped: the trace has the rest."""
    full = (text or "").strip()
    if not full:
        return "> (the builder gave no reason — `just phases` has the envelope)"
    clipped = full[:limit].rstrip() + (" […]" if len(full) > limit else "")
    return "\n".join(f"> {line}" for line in clipped.splitlines())


# ── the label a re-entered issue run lands ───────────────────────────────────

def land_label(cfg: FactoryConfig, main_root, state, code: int) -> None:
    """Move an issue-triggered run's label now that THIS process has ended it.

    The watcher launched the run and saw exit 75 (a gate), so it left the issue
    on `running` — correctly. `asf approve` brings the run back in a process
    the watcher never sees, and that is where it actually ends; without this
    an answered issue sat on `running` forever. Only `running` is removed,
    never a terminal label: the one removal that cannot fail. Never raises
    and never changes the exit code — a tracker that could not be reached is
    a fact about the network, not about the work.
    """
    if state.trigger != "issue" or not state.issue_number or not cfg.issues.enabled:
        return
    if code == hitl.EXIT_WAITING:
        return                              # stopped at the NEXT gate; still claimed
    landed = cfg.issues.states.done if code == 0 else cfg.issues.states.failed
    try:
        result = issues.set_state(main_root, cfg.issues, IssueUpdate(
            number=state.issue_number, project=state.issue_project,
            add_labels=[landed], remove_labels=[cfg.issues.states.running]))
    except Exception as error:                              # noqa: BLE001
        print(f"  #{state.issue_number}: label not moved to {landed} ({error}); "
              f"move it by hand")
        return
    print(f"  #{state.issue_number}: {landed}" if result.ok else
          f"  #{state.issue_number}: label not moved to {landed} — "
          f"{' · '.join(result.notes)}; move it by hand")
