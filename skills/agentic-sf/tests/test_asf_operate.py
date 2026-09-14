"""kill and the worktree verbs, on real sessions."""

from __future__ import annotations

from pathlib import Path

from .asf_helpers import PY_CHECK, adw_id_of, asf, commit_all, envelope, fake_roster, git, wire


def test_a_failed_run_keeps_its_worktree_and_prune_takes_only_what_is_safe(stamped: Path):
    fake_roster(stamped, builder=[{"writes": {"app.py": "1/0\n"},
                                   "envelope": envelope(changed_files=["app.py"],
                                                        commit_message="feat: broken")}] * 3)
    wire(stamped, "test", PY_CHECK)
    commit_all(stamped)
    failed = asf(stamped, "run", "quick", "add app.py")
    assert failed.returncode == 1
    adw_id = adw_id_of(failed)

    listed = asf(stamped, "worktrees", "list")
    assert adw_id in listed.stdout and "fail" in listed.stdout and "[dirty]" in listed.stdout
    kept = asf(stamped, "worktrees", "prune")
    assert kept.returncode == 0 and "nothing to remove" in kept.stdout
    assert (stamped / ".asf-worktrees" / adw_id).is_dir()     # uncommitted work stays
    forced = asf(stamped, "worktrees", "prune", "--force")
    assert forced.returncode == 0 and "removed" in forced.stdout
    assert not (stamped / ".asf-worktrees" / adw_id).exists()
    assert git(stamped, "rev-parse", "--verify", f"asf/{adw_id}")      # the branch is the record
    assert "no run worktrees" in asf(stamped, "worktrees", "list").stdout


def test_kill_on_a_finished_run_signals_nothing(stamped: Path):
    fake_roster(stamped, builder=[{"writes": {"app.py": "ok = 1\n"},
                                   "envelope": envelope(changed_files=["app.py"],
                                                        commit_message="feat: app")}])
    wire(stamped, "test", PY_CHECK)
    commit_all(stamped)
    adw_id = adw_id_of(asf(stamped, "run", "quick", "add app.py"))
    result = asf(stamped, "kill", adw_id)
    assert result.returncode == 0 and "nothing believed alive" in result.stdout
    nobody = asf(stamped, "kill", "deadbeef")
    assert nobody.returncode == 0 and "never started" in nobody.stdout
