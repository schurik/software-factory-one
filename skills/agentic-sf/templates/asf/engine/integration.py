"""Landing a run's branch — merge it, or push it and let a human decide.

A run ends with its work on `asf/<adw_id>` and nowhere else. Getting it from
there onto the base branch is a known command, not a judgement call, so it is a
`kind="code"` phase over this module rather than an agent (SKILL.md rule 8).

Which command is CONFIGURATION, because repositories genuinely disagree: some
merge, some rebase, some let nothing but a reviewed pull request move the base
branch. `worktree.integration.mode` picks; nothing here is opinionated beyond
refusing to do something unsafe.

Two refusals are deliberate and neither is a bug:

  * **Uncommitted work in the run's worktree** stops integration. Whatever the
    agents left behind is not part of a commit yet, and merging the branch would
    silently ship less than the run produced.
  * **A dirty main checkout with the base branch checked out** stops a merge.
    The engineer has uncommitted work there; a merge would either fail halfway
    or bury it. The run's work is safe on its branch, so waiting costs nothing.

When the base branch is NOT checked out anywhere, the merge is done as a ref
update (`git fetch . <branch>:<base>`) — a fast-forward that moves the branch
without touching any working tree at all. Git refuses that when the branch is
checked out, which is exactly the case that needs the real merge above.

A SESSION OUTLIVES THE PROCESS THAT OPENED ITS PULL REQUEST, and that is the
other thing this module has to get right. `just plan-build --adw-id <id>` on a
session whose branch is already under review commits onto that same branch, so
two rules follow, and both used to be broken:

  * **`keep_published` pushes the follow-up commit.** A branch that has been
    pushed once stays pushed, so the pull request shows what the session
    actually contains. Called from every commit phase; it publishes NOTHING on
    its own — no remote-tracking ref, no push — because proposing a branch for
    the first time is the engineer's call and stays `just integrate <adw_id>`.
  * **A second integration updates the PR instead of opening another.** The
    push is what carries the new commits, and `pr create` against a branch that
    already has one is refused by every forge. That refusal used to flip
    `ok=False` — so an integration that HAD just updated the pull request was
    recorded as a failed run, and the engineer was told the opposite of what
    happened.
"""

from __future__ import annotations

import subprocess

from . import artifacts, git_helper
from .data_types import IntegrationRequest, IntegrationResult
from .utils import operator_env


def integrate(run, params: IntegrationRequest) -> IntegrationResult:
    """Land the run's branch per config. Returns evidence, never a claim."""
    config = run.cfg.worktree.integration
    workspace = run.workspace
    mode = params.mode or config.mode

    # An externally triggered run must not be able to move the base branch. The
    # prompt came from whoever can file an issue, not from the engineer's own
    # terminal, so the one thing that cannot be left to config discipline is
    # whether a merge is even reachable on that path. `mode: none` still wins —
    # a repository that wants nothing landed gets nothing landed.
    downgrade = ""
    if (run.trigger == "issue" and run.cfg.issues.force_pr
            and mode == "merge"):
        mode = "pr"
        downgrade = ("issue-triggered run: merge downgraded to pr "
                     "(issues.force_pr) — a stranger's prompt does not "
                     "move the base branch")
    # A run that exists BECAUSE the branch is under review is the one run that
    # must never end the review. There is no config switch beside this one: an
    # engineer who wants the branch merged says so by merging the pull request,
    # which is the whole point of having opened it. `mode: none` still wins.
    elif run.trigger == "pr_review" and mode == "merge":
        mode = "pr"
        downgrade = ("review-triggered run: merge downgraded to pr — this "
                     "branch is already under review, and merging it here "
                     "would land it without the review it is waiting for")

    result = IntegrationResult(mode=mode, branch=workspace.branch,
                               base_ref=workspace.base_ref)
    if downgrade:
        result.notes.append(downgrade)

    if mode == "none":
        result.ok = True
        result.notes.append("integration is disabled (worktree.integration.mode: none)")
        return result
    if not workspace.enabled:
        result.notes.append("this run has no worktree, so it has no branch to land — "
                            "its commits are already on the branch it ran on")
        return result
    if git_helper.is_dirty(workspace.repo_root):
        result.notes.append(f"{workspace.branch} has uncommitted work in "
                            f"{workspace.repo_root} — commit it before integrating, "
                            f"or the merge ships less than the run produced")
        return result

    result.head = git_helper.rev(workspace.repo_root, "HEAD")
    if result.head == workspace.base_commit:
        result.ok = True
        result.notes.append(f"{workspace.branch} has no commits of its own since "
                            f"{workspace.base_ref} — nothing to integrate")
        return result

    return _merge(run, result) if mode == "merge" else _open_pr(run, result, params)


def keep_published(run) -> IntegrationResult:
    """Push new commits onto a branch that is already on the remote. Else no-op.

    This is what stops a pull request from going stale while its session keeps
    working. A commit phase calls it right after committing, and it does exactly
    one thing: if `refs/remotes/<remote>/<branch>` exists — it does only because
    an earlier integration pushed this branch — and the local branch has moved
    past it, push. Otherwise nothing, and nothing is a success: a branch nobody
    published is not this function's business.

    Deliberately NOT `integrate()`. Nothing here can move the base branch, open
    a pull request, or put a branch on the remote for the first time; it keeps a
    published branch honest and refuses every other decision. That is why it is
    safe to call from chains that stop at a committed branch by design.

    A rejected push is a note, never a raise: the commits are on the branch
    either way, and a failed push must not undo a phase that succeeded at
    committing. `ok=False` says the remote did not get them, and the note says
    what git said.
    """
    config = run.cfg.worktree.integration
    workspace = run.workspace
    tree = workspace.main_root            # relative remote urls resolve here — see _open_pr
    result = IntegrationResult(mode="pr", branch=workspace.branch,
                               base_ref=workspace.base_ref, ok=True)

    if not workspace.enabled or not git_helper.has_remote(tree, config.remote):
        return result
    if config.mode == "none":
        # The off switch is total. A repository that has said a run's work stays
        # on its branch does not get a push either — not even onto a branch
        # something else put on the remote.
        result.notes.append("integration is disabled "
                            "(worktree.integration.mode: none)")
        return result
    published = git_helper.remote_tip(tree, config.remote, workspace.branch)
    if not published:
        return result                     # never pushed; publishing is `just integrate`
    result.head = git_helper.rev(workspace.repo_root, "HEAD")
    if result.head == published:
        result.notes.append(f"{config.remote}/{workspace.branch} already has "
                            f"{result.head[:7]}")
        return result

    pushed = git_helper.push(tree, config.remote, workspace.branch)
    if pushed.returncode != 0:
        result.ok = False
        result.notes.append(
            f"{workspace.branch} is published but the new commit could not be "
            f"pushed: {pushed.stderr.strip()[-300:]} — "
            f"{run.pr_url or 'the pull request'} does not show it yet")
        return result
    result.pushed = True
    result.notes.append(f"pushed {result.head[:7]} to {config.remote}/"
                        f"{workspace.branch}"
                        f"{', updating ' + run.pr_url if run.pr_url else ''}")
    return result


def _merge(run, result: IntegrationResult) -> IntegrationResult:
    """Merge the run's branch into the base branch, in the main checkout."""
    config = run.cfg.worktree.integration
    main = run.workspace.main_root
    base = run.workspace.base_ref

    if not git_helper.branch_exists(main, base):
        result.notes.append(f"base ref {base!r} is not a local branch — a detached "
                            f"or remote base cannot be merged into; land "
                            f"{result.branch} by hand, or use mode: pr")
        return result

    if git_helper.current_branch(main) != base:
        # Nobody has it checked out: move the ref, touch no working tree.
        completed = git_helper.update_branch(main, result.branch, base)
        if completed.returncode != 0:
            result.notes.append(
                f"{base} could not be fast-forwarded to {result.branch}: "
                f"{completed.stderr.strip()} — it has moved on since this run "
                f"started, so the merge needs a human")
            return result
        result.ok = True
        result.merged_into = base
        result.notes.append(f"fast-forwarded {base} to {result.branch} without "
                            f"touching a working tree")
        return result

    if git_helper.is_dirty(main):
        result.notes.append(
            f"{main} is on {base} with uncommitted changes — integrating would "
            f"merge into work in progress. Commit or stash there, then run the "
            f"integration again; {result.branch} keeps the run's work meanwhile")
        return result

    message = f"asf({run.adw_id}): merge {result.branch} into {base}"
    try:
        git_helper.merge(main, result.branch, config.merge_flags, message)
    except RuntimeError as error:
        git_helper.merge_abort(main)          # never leave a half-merged checkout
        result.notes.append(f"merge stopped and was aborted: {error}")
        return result
    result.ok = True
    result.merged_into = base
    result.notes.append(f"merged {result.branch} into {base} "
                        f"({' '.join(config.merge_flags) or 'default strategy'})")
    return result


def _pr_body(run, template: str, result: IntegrationResult) -> str:
    """Render the configured PR body, plus `Closes #<n>` on an issue-triggered
    run — the forge closes the issue on merge, so nothing here has to.

    The closing line is automatic rather than something the template has to
    spell out: an issue-triggered run always knows the issue it came from, and
    a PR that silently leaves that issue open is a worse default than one
    operators have to opt out of.

    A BAD TEMPLATE MUST NOT KILL THE RUN. This is operator-authored markdown, and
    `str.format` treats every brace as a field: one JSON snippet, one `${{ }}`,
    one stray `{` and it raises — inside the integrate phase, after the commits
    landed and the branch was pushed, so the chain dies with its work committed
    and `run.finish()` never reached. Every other failure in this module comes
    back as a note; a typo in a config string has no business being the
    exception. The PR gets opened without the template's contribution instead,
    and the note says why — the `Closes #<n>` line still gets added.
    """
    rendered = ""
    if template:
        try:
            rendered = template.format(adw_id=run.adw_id, branch=result.branch,
                                       base_ref=result.base_ref,
                                       issue_number=run.issue_number or "",
                                       issue_url=run.issue_url or "")
        except (KeyError, IndexError, ValueError) as error:
            result.notes.append(
                f"pr_body_template could not be rendered ({type(error).__name__}: "
                f"{error}) — opening the pull request without it. A literal "
                f"brace in that template must be doubled: {{{{ and }}}}")
    closes = f"Closes #{run.issue_number}" if run.issue_number else ""
    return "\n\n".join(part for part in (rendered, closes) if part)


def _open_pr(run, result: IntegrationResult, params: IntegrationRequest) -> IntegrationResult:
    """Push the branch, and optionally ask the forge CLI to open the PR.

    Both commands run in the MAIN checkout, not in the run's worktree. Refs are
    shared between worktrees, so pushing from either sends the same commits —
    but a remote configured with a relative URL (`../mirror.git`, as a local
    clone gets) resolves against the directory git is run in, and from a
    worktree that is one level deeper and somewhere else entirely. The forge CLI
    is run there for the same reason.
    """
    config = run.cfg.worktree.integration
    tree = run.workspace.main_root

    if not git_helper.has_remote(tree, config.remote):
        result.notes.append(f"no remote named {config.remote!r} — nothing to push to. "
                            f"Add one, or use mode: merge")
        return result

    pushed = git_helper.push(tree, config.remote, result.branch)
    if pushed.returncode != 0:
        result.notes.append(f"push to {config.remote} was rejected: "
                            f"{pushed.stderr.strip()}")
        return result
    result.pushed = True
    result.ok = True
    result.notes.append(f"pushed {result.branch} to {config.remote}")

    if not config.open_pr:
        result.notes.append("open_pr is off — the branch is pushed and the pull "
                            "request is the engineer's to open")
        return result

    # The push above is what carries new commits to an EXISTING pull request, so
    # by the time a session integrates a second time the work has already landed
    # where the reviewer looks and there is nothing left to open. Asking the
    # forge to create one anyway gets a refusal, and a refusal here used to fail
    # the run — reporting the opposite of what just happened.
    if run.pr_url:
        result.pr_url = run.pr_url
        result.notes.append(f"the pull request for {result.branch} is already open "
                            f"({run.pr_url}) — the push updated it")
        return result

    argv = [*config.pr_command, "--base", result.base_ref, "--head", result.branch]
    if params.title:
        argv += ["--title", params.title]
    body = params.body or _pr_body(run, config.pr_body_template, result)
    if body:
        # gh rejects --fill together with an explicit --body, and an
        # issue-triggered run always has a body now (its `Closes #<n>` line) —
        # so --fill has to go the moment there is one, not just when an
        # operator configured pr_body_template themselves.
        argv = [flag for flag in argv if flag != "--fill"]
        argv += ["--body", body]
        if "--title" not in argv:
            # --fill would have supplied the title too, and gh refuses to run
            # non-interactively with neither --fill nor --title. The first
            # commit unique to this branch is what --fill itself falls back
            # to for a single-commit PR, and reads fine for a multi-commit
            # one too — a standalone integrate (`adw_integrate.py`, no
            # `params.title`) hits this every time the run also has a body.
            fallback_title = git_helper.first_subject(tree, result.base_ref, result.branch)
            if fallback_title:
                argv += ["--title", fallback_title]
    # The forge CLI is already authenticated in the engineer's shell, so it runs
    # under their environment rather than the ADW's ephemeral `uv run` venv.
    completed = subprocess.run(argv, cwd=tree, env=operator_env(),
                               capture_output=True, text=True)
    output = (completed.stderr or completed.stdout).strip()
    if completed.returncode != 0:
        # The trace is not the only way a branch gets a pull request: someone
        # opened one by hand, an earlier run's db was moved, `open_pr` was
        # turned on afterwards. The forge says so and names the url, and that
        # answer is worth exactly as much as the recorded one — the push already
        # updated whichever PR it is.
        existing = _existing_pr_url(output)
        if not existing:
            result.ok = False
            result.notes.append(f"`{' '.join(config.pr_command)}` failed: "
                                f"{output[-500:]}")
            return result
        result.pr_url = existing
        result.notes.append(f"{result.branch} already had a pull request "
                            f"({existing}) — the push updated it")
    else:
        result.pr_url = next((line for line in completed.stdout.split()
                              if line.startswith("http")), "")
        result.notes.append(f"opened a pull request"
                            f"{': ' + result.pr_url if result.pr_url else ''}")
    # Recorded here, not in the ADW: this is the only place a pr url exists, and
    # a chain that landed a branch must not be able to forget where it went. In
    # memory too, so a later phase of THIS process — and `keep_published` — can
    # name the pull request without going back to the db.
    run.pr_url = result.pr_url or run.pr_url
    run.tracer.session_pr(run.adw_id, result.pr_url)
    # And in the session's own file: the review watcher reads `pr_url` from
    # run.json to know which sessions became pull requests, and a db is not
    # something it may assume.
    if run.pr_url:
        artifacts.update_run(run.session_dir, pr_url=run.pr_url)
    return result


def _existing_pr_url(output: str) -> str:
    """The url a forge CLI names when it refuses to open a second pull request.

    `gh` answers `a pull request for branch "x" into branch "main" already
    exists:` followed by the url, and other CLIs word it their own way — so the
    match is on the phrase family, not on one vendor's sentence, and a url is
    required before the refusal is read as "there is already one". Anything else
    stays a failure: this must not turn an authentication error into a success.
    """
    lowered = output.lower()
    if not any(phrase in lowered for phrase in
               ("already exists", "already open", "existing pull request")):
        return ""
    return next((word.rstrip(".,);") for word in output.split()
                 if word.startswith("http")), "")
