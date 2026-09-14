"""The two pollers, in-process against the fake forge, with the launch replaced
by a chosen exit code. Everything between listing an item and moving its label
is the code that ships. Three outcomes, not two: exit 75 is a person who has
not answered yet, and reading it as failure tells the reporter the opposite
of what happened."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine import artifacts, factory, watch
from engine.data_types import RunState, WaitingFor

from .asf_helpers import (asf, fake_roster, forge, forge_calls, forge_data, issue_json,
                          set_config)

CONFIG = "asf/factory.yaml"


@pytest.fixture
def tracked(stamped: Path, monkeypatch):
    """A stamped repo on the fake forge with both watchers enabled. Returns
    (cfg, listing) — `listing(*numbers)` sets what the next poll lists."""
    monkeypatch.chdir(stamped)
    fake_roster(stamped, builder=[{"envelope": {"status": "success"}}])
    base = forge(stamped)
    commands = {"list_command": [*base, "list"], "comment_command": [*base, "comment"],
                "state_command": [*base, "edit"]}
    set_config(stamped,
               issues={"enabled": True, "project": "acme/widgets",
                       "fetch_command": [*base, "view"], "route": {"asf:ship": "issue"},
                       **commands},
               pull_requests={"enabled": True, "project": "acme/widgets",
                              "graphql_command": [*base, "graphql"], **commands})
    forge_data(stamped, "listing.json", [])

    def listing(*entries) -> None:
        forge_data(stamped, "listing.json", list(entries))
    return factory.load(CONFIG), listing


def queued(number: int, *labels: str) -> dict:
    entry = issue_json(number, labels=labels or ("asf:queued", "asf:ship"))
    return {k: entry[k] for k in ("number", "title", "labels", "author")}


def open_pr(number: int, branch: str = "asf/1d5e4c0e", *labels: str, draft: bool = False) -> dict:
    return {"number": number, "headRefName": branch, "isDraft": draft,
            "labels": [{"name": name} for name in labels]}


def labels(repo: Path, number: int) -> tuple[list[str], list[str]]:
    """(added, removed) across every edit the watcher made to one item."""
    added, removed = [], []
    for call in forge_calls(repo):
        if call[0] != "edit" or str(number) not in call:
            continue
        for flag, value in zip(call, call[1:]):
            (added if flag == "--add-label" else removed if flag == "--remove-label" else []
             ).append(value)
    return added, removed


@pytest.mark.parametrize("code,expected", [(0, "asf:done"), (1, "asf:failed")])
def test_a_finished_issue_run_moves_the_label_to_its_outcome(tracked, stamped, monkeypatch,
                                                             code, expected):
    cfg, listing = tracked
    listing(queued(42))
    launched = []
    monkeypatch.setattr(watch, "launch", lambda *a: launched.append(a[1:3]) or code)
    assert watch.issues_once(cfg, CONFIG) == 0
    assert launched == [("issue", 42)]
    added, removed = labels(stamped, 42)
    assert added == ["asf:running", expected] and removed == ["asf:queued", "asf:running"]


def test_a_run_stopped_at_a_gate_is_left_running_not_failed(tracked, stamped, monkeypatch, capsys):
    cfg, listing = tracked
    listing(queued(42))
    monkeypatch.setattr(watch, "launch", lambda *a: watch.EXIT_WAITING)
    assert watch.issues_once(cfg, CONFIG) == 0
    added, removed = labels(stamped, 42)
    assert added == ["asf:running"] and removed == ["asf:queued"]
    assert "stopped for a human at a gate" in capsys.readouterr().out


def test_the_claim_clears_a_previous_run_s_verdict_but_never_the_routing_label(tracked, stamped,
                                                                              monkeypatch):
    cfg, listing = tracked
    listing(queued(42, "asf:queued", "asf:failed", "asf:ship"))
    monkeypatch.setattr(watch, "launch", lambda *a: 0)
    watch.issues_once(cfg, CONFIG)
    added, removed = labels(stamped, 42)
    assert removed[:2] == ["asf:failed", "asf:queued"] and "asf:ship" not in removed
    assert added == ["asf:running", "asf:done"]


def test_an_issue_without_a_routing_label_is_left_alone(tracked, stamped, monkeypatch):
    cfg, listing = tracked
    listing(queued(42, "asf:queued"))
    launched = []
    monkeypatch.setattr(watch, "launch", lambda *a: launched.append(a) or 0)
    watch.issues_once(cfg, CONFIG)
    assert launched == [] and forge_calls(stamped) == []


def test_a_failed_review_is_marked_and_a_label_that_will_not_stick_is_held(tracked, stamped,
                                                                          monkeypatch, capsys):
    cfg, listing = tracked
    listing(open_pr(72))
    monkeypatch.setattr(watch, "has_work", lambda *a: True)
    monkeypatch.setattr(watch, "reap", lambda *a: 0)
    launched = []
    monkeypatch.setattr(watch, "launch", lambda *a: launched.append(a[2]) or 1)
    watch._HELD.clear()

    assert watch.prs_once(cfg, CONFIG) == 0
    assert labels(stamped, 72)[0] == ["asf:pr-failed"] and watch._HELD == set()

    forge_data(stamped, "refuse.json", ["edit"])
    watch.prs_once(cfg, CONFIG)
    watch.prs_once(cfg, CONFIG)                 # held: not bought a second time
    assert launched == [72, 72] and watch._HELD == {("acme/widgets", 72)}
    assert "did not stick" in capsys.readouterr().out
    watch._HELD.clear()


def test_a_draft_a_marked_or_a_waiting_pull_request_is_skipped(tracked, stamped, monkeypatch):
    cfg, listing = tracked
    sessions = artifacts.sessions_root(stamped, cfg.defaults.data_dir)
    (sessions / "cafed00d").mkdir(parents=True)
    artifacts.write_run(sessions / "cafed00d", RunState(
        adw_id="cafed00d", status="waiting", pr_url="https://forge/acme/widgets/pull/74",
        waiting_for=WaitingFor(gate="plan", round=1)))
    listing(open_pr(72, draft=True), open_pr(73, "asf/x", "asf:pr-failed"), open_pr(74),
            open_pr(75, "feature/by-hand"))
    monkeypatch.setattr(watch, "has_work", lambda *a: True)
    monkeypatch.setattr(watch, "reap", lambda *a: 0)
    launched = []
    monkeypatch.setattr(watch, "launch", lambda *a: launched.append(a[2]) or 0)
    watch.prs_once(cfg, CONFIG)
    assert launched == []
    assert watch.waiting_on(cfg, stamped, 74) == "cafed00d"


def test_the_launcher_tells_the_run_its_terminal_is_not_the_run_s(stamped, monkeypatch):
    seen = {}

    def fake_run(argv, cwd=None, env=None, **kwargs):
        seen["argv"], seen["env"] = argv, env or {}
        return type("Completed", (), {"returncode": 0})()
    monkeypatch.setattr(watch.subprocess, "run", fake_run)
    watch.launch(CONFIG, "issue", 42, stamped)
    assert seen["env"].get("ASF_UNATTENDED") == "1"
    assert seen["argv"][1:] == ["asf/asf.py", "--config", CONFIG, "run", "issue", "42"]


def test_a_watcher_refuses_a_route_to_a_workflow_with_the_wrong_input(stamped):
    fake_roster(stamped, builder=[{"envelope": {"status": "success"}}])
    set_config(stamped, issues={"enabled": True, "project": "acme/widgets",
                                "route": {"asf:quick": "quick"}})
    result = asf(stamped, "issues", "once")
    assert result.returncode != 0
    assert "takes input: prompt" in result.stderr
    status = asf(stamped, "issues", "status")
    assert status.returncode == 0 and "asf:quick" in status.stdout


def test_status_reads_the_heartbeats_and_probes_the_pids(tracked, stamped, monkeypatch):
    cfg, listing = tracked
    monkeypatch.setattr(watch, "launch", lambda *a: 0)
    watch.issues_once(cfg, CONFIG)
    beat = json.loads((stamped / "asf" / "data" / "watchers" / "issues.json").read_text())
    assert beat["status"] == "polling" and beat["kind"] == "issues"
    shown = asf(stamped, "status")
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert "issues  polling" in shown.stdout           # this process's pid is alive
    assert "prs     never started here" in shown.stdout
    assert "runs in flight: 0" in shown.stdout
