"""`input: issue` and `input: pr`, end to end on the fake harness and a fake forge.

The forge commands are config, so a python script standing in for `gh` is a
supported deployment, not a mock: everything between reading the issue and
commenting the outcome is the code that ships.
"""

from __future__ import annotations

import json
from pathlib import Path

from .asf_helpers import (PY_CHECK, adw_id_of, asf, commit_all, envelope, fake_roster, forge,
                          forge_calls, forge_data, git, issue_json, phase_names, pr_json,
                          run_state, session_dir, set_config, wire, with_origin)

ID = "1d5e4c0e"
ID2 = "2d5e4c0e"


def scout_reply(repo: Path, adw_id: str = ID) -> dict:
    findings = session_dir(repo, adw_id) / "context_handoff" / "scout_findings.md"
    return {"writes": {str(findings): "# Findings\n\n- app.py: not there yet.\n"},
            "envelope": envelope(findings=[{"file": "app.py", "note": "missing"}],
                                 artifacts=[str(findings)])}


def plan_reply() -> dict:
    return {"writes": {"specs/plan.md": "# Plan\n"},
            "envelope": envelope(artifacts=["specs/plan.md"], commit_message="docs: plan")}


def build_reply(content: str, message: str) -> dict:
    return {"writes": {"app.py": content},
            "envelope": envelope(changed_files=["app.py"], commit_message=message)}


def review_reply() -> dict:
    return {"envelope": envelope(approved=True, blocking=[], findings=[], artifacts=[])}


def document_reply() -> dict:
    return {"writes": {"app_docs/app.md": "# app\n"},
            "envelope": envelope(artifacts=["app_docs/app.md"], document_path="app_docs/app.md",
                                 documented_files=["app.py"], commit_message="docs: app")}


def tracker(repo: Path, **issues) -> list[str]:
    base = forge(repo)
    set_config(repo, issues={
        "enabled": True, "project": "acme/widgets",
        "fetch_command": [*base, "view"], "list_command": [*base, "list"],
        "comment_command": [*base, "comment"], "state_command": [*base, "edit"],
        "route": {"asf:ship": "issue"}, **issues})
    return base


def reviewed(repo: Path, **pull_requests) -> list[str]:
    base = forge(repo)
    set_config(repo, pull_requests={
        "enabled": True, "project": "acme/widgets",
        "list_command": [*base, "list"], "comment_command": [*base, "comment"],
        "state_command": [*base, "edit"], "graphql_command": [*base, "graphql"],
        **pull_requests})
    return base


# ── issue ────────────────────────────────────────────────────────────────────

def test_an_issue_is_scouted_planned_shipped_and_reported_as_a_pull_request(stamped: Path):
    fake_roster(stamped, scout=[scout_reply(stamped)], planner=[plan_reply()],
                builder=[build_reply("ok = 1\n", "feat: app")], reviewer=[review_reply()],
                documenter=[document_reply()])
    wire(stamped, "test", PY_CHECK)
    base = tracker(stamped)
    forge_data(stamped, "issue.json", issue_json(42))
    # `mode: merge` in the config, and it must NOT be honoured on this input.
    set_config(stamped, worktree={"integration": {"mode": "merge", "open_pr": True,
                                                  "pr_command": [*base, "pr-create"]}})
    with_origin(stamped)
    commit_all(stamped)
    before = git(stamped, "rev-parse", "main")

    result = asf(stamped, "run", "issue", "42", "--adw-id", ID)
    assert result.returncode == 0, result.stdout + result.stderr

    assert phase_names(stamped, ID) == [
        "issue", "scout", "plan", "commit_plan", "implement", "verify_1", "review_1",
        "commit_implement", "changes", "document", "commit_document", "integrate", "report"]
    state = run_state(stamped, ID)
    assert state["trigger"] == "issue" and state["issue_number"] == 42
    assert state["pr_url"] == "https://forge/acme/widgets/pull/9"
    # A stranger's prompt does not move the base branch: merge became a pull request.
    assert git(stamped, "rev-parse", "main") == before
    assert git(stamped, "ls-remote", "--heads", "origin", f"asf/{ID}")
    # The reporter's words reached the scout and the planner as an ARTIFACT, framed;
    # the planner got the issue's envelope with the scout's findings appended.
    prompts = session_dir(stamped, ID)
    scout_brief = (prompts / "scout" / "prompts" / "user.md").read_text()
    planner_brief = (prompts / "planner" / "prompts" / "user.md").read_text()
    assert "issue.md" in scout_brief and "EVIDENCE TO WORK FROM" in scout_brief
    assert "500" not in scout_brief                    # the body is in the file, not the prompt
    assert "issue.md" in planner_brief and "scout_findings.md" in planner_brief
    assert "recon, not a plan" in planner_brief
    body = (prompts / "context_handoff" / "issue.md").read_text()
    assert "USER'S DESCRIPTION OF A PROBLEM" in body and "returns 500" in body
    # The tracker heard the outcome, with the pull request and the resume handle.
    comments = [call for call in forge_calls(stamped) if call[0] == "comment"]
    assert len(comments) == 1 and "42" in comments[0]
    said = comments[0][comments[0].index("--body") + 1]
    assert "**asf**" in said and "ready for review" in said
    assert "https://forge/acme/widgets/pull/9" in said and ID in said


def test_an_untrusted_reporter_costs_nothing_and_a_failed_run_still_reports(stamped: Path):
    fake_roster(stamped, scout=[scout_reply(stamped, ID2)], planner=[plan_reply()],
                builder=[build_reply("1/0\n", "feat: broken")], reviewer=[review_reply()],
                documenter=[document_reply()])
    wire(stamped, "test", PY_CHECK)
    tracker(stamped, trusted_authors=["alice"])
    forge_data(stamped, "issue.json", issue_json(42, author="bob"))
    commit_all(stamped)

    refused = asf(stamped, "run", "issue", "42", "--adw-id", ID)
    assert refused.returncode != 0
    assert "bob is not in issues.trusted_authors" in refused.stdout + refused.stderr
    assert phase_names(stamped, ID) == ["issue"]       # no agent was spawned or paid for
    assert forge_calls(stamped) == []                  # and nothing was said

    # A trusted reporter whose build never goes green hears where it stopped.
    forge_data(stamped, "issue.json", issue_json(43, author="alice"))
    set_config(stamped, worktree={"integration": {"mode": "none"}})
    failed = asf(stamped, "run", "issue", "43", "--adw-id", ID2)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    names = phase_names(stamped, ID2)
    assert names[-1] == "report" and "commit_implement" not in names
    said = [call for call in forge_calls(stamped) if call[0] == "comment"][-1]
    assert "could not finish" in said[said.index("--body") + 1]


def scripted(repo: Path) -> None:
    """A roster that validates; none of these replies is ever spent."""
    fake_roster(repo, scout=[{"envelope": envelope()}], planner=[{"envelope": envelope()}],
                builder=[{"envelope": envelope()}], reviewer=[{"envelope": envelope()}],
                documenter=[{"envelope": envelope()}])


def test_a_workflow_with_input_issue_takes_a_number_and_check_says_which_input(stamped: Path):
    scripted(stamped)
    result = asf(stamped, "run", "issue", "add a health endpoint")
    assert result.returncode != 0 and "input: issue" in result.stderr
    assert not (stamped / "asf" / "data" / "sessions").exists()      # refused before a session


# ── pull request ─────────────────────────────────────────────────────────────

def opened_branch(stamped: Path) -> str:
    """A session whose branch is on the remote — what a pull request is made of."""
    fake_roster(stamped, builder=[build_reply("ok = 1\n", "feat: app")])
    wire(stamped, "test", PY_CHECK)
    with_origin(stamped)
    commit_all(stamped)
    first = asf(stamped, "run", "quick", "add app.py", "--adw-id", ID)
    assert first.returncode == 0, first.stdout + first.stderr
    git(stamped, "push", "-q", "-u", "origin", f"asf/{ID}")
    return f"asf/{ID}"


def thread(body: str, **fields) -> dict:
    return {"body": body, **fields}


def test_a_review_is_answered_on_the_branch_under_review_and_the_thread_resolved(stamped: Path):
    branch = opened_branch(stamped)
    fake_roster(stamped, builder=[build_reply("ok = 2\n", "fix: ok is 2, as reviewed")])
    reviewed(stamped)
    forge_data(stamped, "pr.json", pr_json(72, branch, [thread("make ok 2")]))
    commit_all(stamped)

    result = asf(stamped, "run", "pr-review", "72")
    assert result.returncode == 0, result.stdout + result.stderr
    assert adw_id_of(result) == ID                     # the pull request named its session
    names = phase_names(stamped, ID)
    assert names[-5:] == ["pr", "implement", "verify_1", "commit_implement", "report"]
    # The builder was given the review task, not the implement task.
    brief = (session_dir(stamped, ID) / "builder" / "prompts" / "user.md").read_text()
    assert brief.startswith("# Address review feedback") and "pr_review.md" in brief
    assert "make ok 2" not in brief                    # the ask is in the file, not the prompt
    # Pushed onto the branch the reviewer is looking at; no second pull request.
    assert git(stamped, "rev-parse", branch) == git(stamped, "rev-parse", f"origin/{branch}")
    assert git(stamped, "log", "-1", "--format=%s", branch) == "fix: ok is 2, as reviewed"
    calls = forge_calls(stamped)
    replies = [c for c in calls if c[0] == "graphql" and any("addPullRequestReviewThreadReply" in a for a in c)]
    resolves = [c for c in calls if c[0] == "graphql" and any("resolveReviewThread" in a for a in c)]
    assert len(replies) == 1 and len(resolves) == 1
    assert "addressed in the commit above" in next(a for a in replies[0] if a.startswith("body="))
    summary = [c for c in calls if c[0] == "comment"][-1]
    assert "answered 1 review thread(s)" in summary[summary.index("--body") + 1]


def test_a_builder_that_declines_on_purpose_keeps_the_thread_open_with_its_reason(stamped: Path):
    branch = opened_branch(stamped)
    fake_roster(stamped, builder=[{"envelope": envelope(
        summary="Declined: the thread asks for a dependency this branch decided against.",
        changed_files=[], commit_message="")}])
    reviewed(stamped)
    forge_data(stamped, "pr.json", pr_json(72, branch, [thread("add left-pad")]))
    commit_all(stamped)
    tip = git(stamped, "rev-parse", branch)

    result = asf(stamped, "run", "pr-review", "72")
    assert result.returncode == 0, result.stdout + result.stderr   # a judgement, not a failure
    assert git(stamped, "rev-parse", branch) == tip                # nothing committed
    calls = forge_calls(stamped)
    replies = [c for c in calls if c[0] == "graphql" and any("addPullRequestReviewThreadReply" in a for a in c)]
    resolves = [c for c in calls if c[0] == "graphql" and any("resolveReviewThread" in a for a in c)]
    assert len(replies) == 1 and resolves == []
    reply = next(a for a in replies[0] if a.startswith("body="))
    assert "changed nothing, on purpose" in reply and "> Declined: the thread asks" in reply
    summary = [c for c in calls if c[0] == "comment"][-1]
    assert "changed nothing" in summary[summary.index("--body") + 1]


def test_a_red_suite_is_an_unfinished_review_that_resolves_nothing(stamped: Path):
    branch = opened_branch(stamped)
    fake_roster(stamped, builder=[build_reply("1/0\n", "fix: broken")] * 4)
    reviewed(stamped)
    forge_data(stamped, "pr.json", pr_json(72, branch, [thread("break it")]))
    commit_all(stamped)

    result = asf(stamped, "run", "pr-review", "72")
    assert result.returncode == 1
    calls = forge_calls(stamped)
    assert not [c for c in calls if c[0] == "graphql" and any("resolveReviewThread" in a for a in c)]
    reply = next(a for c in calls if c[0] == "graphql" for a in c if a.startswith("body="))
    assert "could not finish" in reply


def test_nothing_to_answer_is_a_success_that_spends_nothing(stamped: Path):
    branch = opened_branch(stamped)
    reviewed(stamped)
    forge_data(stamped, "pr.json", pr_json(72, branch, []))
    commit_all(stamped)
    result = asf(stamped, "run", "pr-review", "72")
    assert result.returncode == 0, result.stdout + result.stderr
    assert phase_names(stamped, ID)[-1] == "pr" and "no open review threads" in result.stdout


def test_a_closed_or_foreign_pull_request_is_refused_before_a_session_exists(stamped: Path):
    scripted(stamped)
    reviewed(stamped)
    forge_data(stamped, "pr.json", pr_json(72, f"asf/{ID}", [thread("x")], state="MERGED"))
    merged = asf(stamped, "run", "pr-review", "72")
    assert merged.returncode == 2 and "is merged" in merged.stderr
    forge_data(stamped, "pr.json", pr_json(72, "feature/by-hand", [thread("x")]))
    foreign = asf(stamped, "run", "pr-review", "72")
    assert foreign.returncode == 2 and "does not start with `asf/`" in foreign.stderr
    forge_data(stamped, "pr.json", pr_json(72, f"asf/{ID}", [thread("x")]))
    other = asf(stamped, "run", "pr-review", "72", "--adw-id", "deadbeef")
    assert other.returncode == 2 and "belongs to" in other.stderr
    assert not (stamped / "asf" / "data" / "sessions").exists()
    assert json.loads((stamped / ".forge" / "calls.json").read_text()) == [] \
        if (stamped / ".forge" / "calls.json").exists() else True
