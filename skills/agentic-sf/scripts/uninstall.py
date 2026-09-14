#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""uninstall — delete the stamped factory out of a repo. The skill is untouched.

Usage:
    uv run <skill>/scripts/uninstall.py [--dry-run] [--yes] [--branches] [--force]
                                        [--config asf/factory.yaml]

The inverse of `install.py`, and the only thing here that is not reversible.
Run it from the **target repo root** — the cwd is what gets emptied, never the
skill, which keeps every generator and can stamp the factory back tomorrow.

What goes: `asf/` entire (the engine, the stages, the agents and workflows you
own, and the whole run record under `asf/data/`), the per-run worktrees and
their git metadata, `.env.sample`, the stamped justfile, and the
`# agentic-sf runtime` block from `.gitignore`.

Three files live in a namespace the repository had before the factory arrived —
`justfile`, `.env`, `.gitignore` — so each is compared against what was stamped
and **kept the moment it differs**. A `.env` holding your API keys and a
justfile holding your own recipes are not the factory's to delete.

The trace db goes with the run record. It lives under `asf/data/` unless
`observability.db` was pointed elsewhere, in which case that file (and its
`-wal`/`-shm` siblings) is deleted by name.

Refuses while anything is still running: a half-deleted factory under a live
run is the one state worse than either end of this. `asf kill <adw_id>` and
ctrl-c on `asf up` first, or `--force` to say you have.

Stdlib only, and it imports nothing from `asf/` — this has to work on a
factory that is already broken, which is when it is most wanted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install                                     # noqa: E402  (the stamp list)

SKILL_ROOT = install.SKILL_ROOT
TEMPLATES = install.TEMPLATES

CONFIG = "asf/factory.yaml"
DEFAULT_DB = "asf/data/asf.db"
DEFAULT_DATA_DIR = "asf/data"
DEFAULT_WORKTREE_DIR = ".asf-worktrees"
DEFAULT_BRANCH_PREFIX = "asf/"
GITIGNORE_HEADER = "# agentic-sf runtime"
ENV_LINE = "ASF_SKILL="

# Runtime, never stamped: excluded when counting "files the skill never wrote".
RUNTIME_NAMES = {"data", "__pycache__"}


# ── reading the repo, without the factory's own code ─────────────────────────

def scalar(text: str, section: str, key: str) -> str | None:
    """One `key:` from one top-level `section:` of the config. No yaml here —
    a dependency on pyyaml to read four strings would cost this script the one
    property that matters: running on a repo whose factory no longer works."""
    body = re.search(rf"^{section}:\s*$\n((?:^[ \t].*$\n?|^\s*$\n?)*)", text, re.MULTILINE)
    if not body:
        return None
    found = re.search(rf"^\s+{key}:\s*(.+?)\s*(?:#.*)?$", body.group(1), re.MULTILINE)
    return found.group(1).strip().strip("\"'") if found else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def live_processes(root: Path, data_dir: str) -> list[str]:
    """Runs and watchers the RECORD believes are alive AND whose pid still is."""
    def read(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    alive = []
    sessions = root / data_dir / "sessions"
    if sessions.is_dir():
        for child in sorted(sessions.iterdir()):
            state = read(child / "run.json") if child.is_dir() else {}
            pid = state.get("pid") or 0
            if state.get("status") == "running" and pid and _pid_alive(pid):
                adw_id = state.get("adw_id") or child.name
                alive.append(f"run {adw_id} (pid {pid}) — stop it with: asf kill {adw_id}")
    watchers = root / data_dir / "watchers"
    if watchers.is_dir():
        for child in sorted(watchers.glob("*.json")):
            row = read(child)
            pid = row.get("pid") or 0
            if row.get("status") in ("polling", "working") and pid and _pid_alive(pid):
                alive.append(f"{row.get('kind') or child.stem} watcher (pid {pid}) — "
                             f"ctrl-c the `asf up` that owns it")
    return alive


def git(root: Path, *argv: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def run_worktrees(root: Path, wt_dir: str, prefix: str) -> list[Path]:
    """From git, not the directory listing: `rm -rf` leaves `.git/worktrees`
    claiming trees that are not there, and every later add for a reused id fails."""
    code, out = git(root, "worktree", "list", "--porcelain")
    if code != 0:
        return []
    container = (root / wt_dir).resolve()
    found, path = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1]).resolve()
        elif line.startswith("branch ") and path is not None:
            branch = line.split(" ", 1)[1].removeprefix("refs/heads/")
            if branch.startswith(prefix) or container in path.parents:
                found.append(path)
            path = None
    return found


def run_branches(root: Path, prefix: str) -> list[str]:
    code, out = git(root, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}*")
    return out.splitlines() if code == 0 and out else []


# ── what the skill stamped, and what it did not ──────────────────────────────

def stamped_targets(root: Path) -> set[Path]:
    targets: set[Path] = set()

    def walk(src: Path, dest: Path) -> None:
        if src.is_dir():
            for child in src.iterdir():
                if child.name != "__pycache__":
                    walk(child, dest / child.name)
        elif src.exists():
            targets.add(dest)

    walk(TEMPLATES / "asf", root / "asf")
    targets.add(root / CONFIG)
    return targets


def unstamped_files(root: Path) -> list[Path]:
    """Files under `asf/` the skill never wrote: your workflows, your agents.
    Deleted too — `asf/` is the factory — but named before the question."""
    stamped = stamped_targets(root)
    extra = []
    for path in sorted((root / "asf").rglob("*")):
        if path.is_dir() or path in stamped or path.suffix == ".pyc":
            continue
        if RUNTIME_NAMES & set(path.relative_to(root / "asf").parts):
            continue
        extra.append(path)
    return extra


def shipped_env_samples() -> list[str]:
    return [(install.HARNESSES / h / "env.sample").read_text()
            for h in install.harness_names()
            if (install.HARNESSES / h / "env.sample").is_file()]


def is_as_stamped(path: Path, candidates: list[str]) -> bool:
    return path.is_file() and any(path.read_text() == text for text in candidates)


def env_is_only_stamped(env: Path) -> bool:
    """The sample plus the `ASF_SKILL=` line the installer wrote, and nothing else."""
    if not env.is_file():
        return False

    def strip(text: str) -> list[str]:
        return [line for line in text.splitlines() if not line.startswith(ENV_LINE)]
    return any(strip(env.read_text()) == strip(sample) for sample in shipped_env_samples())


# ── the plan ─────────────────────────────────────────────────────────────────

def build_plan(root: Path, db: Path, wt_dir: str, prefix: str) -> dict:
    delete: list[Path] = []
    keep: list[str] = []

    if (root / "asf").is_dir():
        delete.append(root / "asf")
    env_sample = root / ".env.sample"
    if env_sample.is_file():
        if is_as_stamped(env_sample, shipped_env_samples()):
            delete.append(env_sample)
        else:
            keep.append(".env.sample — not one this skill ships today; either it is yours "
                        "or it came from an older version. Read it, then delete it yourself")
    stamped_justfile = (TEMPLATES / "justfile").read_text()
    for name in ("justfile", "asf.justfile"):
        justfile = root / name
        if not justfile.is_file():
            continue
        if is_as_stamped(justfile, [stamped_justfile]):
            delete.append(justfile)
        elif justfile.read_text().startswith(install.JUSTFILE_MARK):
            keep.append(f"{name} — a stamped one that has since diverged (your recipes, or "
                        f"an older version of this skill); delete or strip it yourself")
        elif name == "justfile":
            keep.append("justfile — this repo's own; install.py never wrote to it")
    env = root / ".env"
    if env.is_file():
        if env_is_only_stamped(env):
            delete.append(env)
        else:
            keep.append(".env — it holds values the sample does not; only the ASF_SKILL "
                        "line is removed")

    # Under asf/ the db goes with the directory; elsewhere it is named and deleted.
    db_files = [] if (root / "asf") in db.parents else [
        p for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"))
        if p.is_file()]

    _, dropped, precise = gitignore_without_block(root / ".gitignore")
    return {
        "delete": delete, "keep": keep,
        "worktrees": run_worktrees(root, wt_dir, prefix), "worktree_dir": root / wt_dir,
        "branches": run_branches(root, prefix),
        "unstamped": unstamped_files(root) if (root / "asf").is_dir() else [],
        "gitignore": dropped, "gitignore_precise": precise,
        "env": env, "db_files": db_files,
    }


def describe(plan: dict, root: Path, branches: bool) -> None:
    print(f"uninstall agentic-sf from {root}\n")
    if plan["delete"]:
        print("  delete:")
        for path in plan["delete"]:
            count = (f"  ({sum(1 for p in path.rglob('*') if p.is_file())} files)"
                     if path.is_dir() else "")
            print(f"    - {path.relative_to(root)}{count}")
    for path in plan["db_files"]:
        print(f"    - {path.relative_to(root)}  (the run record)")
    if plan["worktrees"]:
        print(f"  remove {len(plan['worktrees'])} run worktree(s), git metadata included:")
        for path in plan["worktrees"]:
            print(f"    - {path}")
    if plan["gitignore"]:
        print(f"  .gitignore: drop the agentic-sf runtime block ({len(plan['gitignore'])} entries)")
    elif not plan["gitignore_precise"]:
        print(f"  .gitignore: left alone — it has the factory's entries but not the "
              f"`{GITIGNORE_HEADER}` header, so which are yours is a guess. Remove by hand.")
    if plan["branches"]:
        verb = "delete" if branches else "KEEP (pass --branches to delete)"
        print(f"  {len(plan['branches'])} run branch(es): {verb}")
        for name in plan["branches"][:10]:
            print(f"    - {name}")
    if plan["keep"]:
        print("  kept, and why:")
        for note in plan["keep"]:
            print(f"    · {note}")
    if plan["unstamped"]:
        print(f"\n  {len(plan['unstamped'])} file(s) under asf/ this skill never stamped — "
              f"your own work, deleted with the rest:")
        for path in plan["unstamped"][:10]:
            print(f"    ! {path.relative_to(root)}")


# ── doing it ─────────────────────────────────────────────────────────────────

def gitignore_without_block(gitignore: Path) -> tuple[list[str], list[str], bool]:
    """(lines that stay, lines that go, was the block found). Only the run of
    known entries FOLLOWING the header the installer wrote is taken — half of
    them (`.env`, `__pycache__/`) are ones a repo plausibly had already."""
    if not gitignore.is_file():
        return [], [], True
    lines = gitignore.read_text().splitlines()
    kept, dropped, index, found = [], [], 0, False
    while index < len(lines):
        if lines[index].strip() != GITIGNORE_HEADER:
            kept.append(lines[index])
            index += 1
            continue
        found = True
        index += 1
        while index < len(lines) and lines[index] in install.GITIGNORE_ENTRIES:
            dropped.append(lines[index])
            index += 1
    # A trailing blank the installer wrote before its header goes with it.
    while kept and dropped and not kept[-1].strip():
        kept.pop()
    return kept, dropped, found or not any(line in install.GITIGNORE_ENTRIES for line in lines)


def strip_gitignore(root: Path, plan: dict, removed: list[str]) -> None:
    gitignore = root / ".gitignore"
    if not plan["gitignore"]:
        return
    kept, dropped, _ = gitignore_without_block(gitignore)
    if not any(line.strip() for line in kept):
        gitignore.unlink()
        removed.append(".gitignore (nothing left in it)")
        return
    gitignore.write_text("\n".join(kept).rstrip("\n") + "\n")
    removed.append(f".gitignore (-{len(dropped)} entries)")


def strip_env(env: Path, removed: list[str]) -> None:
    if not env.is_file():
        return
    lines = env.read_text().splitlines()
    kept = [line for line in lines if not line.startswith(ENV_LINE)]
    if len(kept) != len(lines):
        env.write_text("\n".join(kept).rstrip("\n") + "\n")
        removed.append(".env (-1 ASF_SKILL line)")


def execute(plan: dict, root: Path, branches: bool) -> list[str]:
    removed: list[str] = []
    for path in plan["worktrees"]:
        code, out = git(root, "worktree", "remove", "--force", str(path))
        if code != 0:
            shutil.rmtree(path, ignore_errors=True)
            print(f"  git would not remove {path} ({out}) — the directory is gone, "
                  f"pruning the metadata")
        removed.append(f"worktree {path}")
    if plan["worktrees"]:
        git(root, "worktree", "prune")
    container = plan["worktree_dir"]
    if container.is_dir():
        shutil.rmtree(container, ignore_errors=True)
        removed.append(str(container.relative_to(root)))
    if branches:
        for name in plan["branches"]:
            code, out = git(root, "branch", "-D", name)
            removed.append(f"branch {name}" if code == 0 else f"branch {name} NOT deleted ({out})")
    for path in plan["delete"]:
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        removed.append(str(path.relative_to(root)))
    for path in plan["db_files"]:
        path.unlink(missing_ok=True)
        removed.append(str(path.relative_to(root)))
        parent = path.parent
        while parent != root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    strip_gitignore(root, plan, removed)
    if plan["env"] not in plan["delete"]:
        strip_env(plan["env"], removed)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation; required with no terminal")
    parser.add_argument("--branches", action="store_true",
                        help="also delete the asf/<adw_id> branches — the only copy of any "
                             "run that never landed")
    parser.add_argument("--force", action="store_true",
                        help="uninstall even while runs or watchers are alive")
    parser.add_argument("--config", default=CONFIG)
    args = parser.parse_args()
    root = Path.cwd().resolve()

    if root == SKILL_ROOT or SKILL_ROOT in root.parents:
        print(f"refusing: {root} is the skill, or is inside it ({SKILL_ROOT}).\n"
              f"run this from the repo the factory was stamped into.", file=sys.stderr)
        return 1
    text = (root / args.config).read_text() if (root / args.config).is_file() else ""
    wt_dir = scalar(text, "worktree", "dir") or DEFAULT_WORKTREE_DIR
    prefix = scalar(text, "worktree", "branch_prefix") or DEFAULT_BRANCH_PREFIX
    db = root / (scalar(text, "observability", "db") or DEFAULT_DB)
    data_dir = scalar(text, "defaults", "data_dir") or DEFAULT_DATA_DIR

    for path in run_worktrees(root, wt_dir, prefix):
        if path == root or path in root.parents:
            print(f"refusing: this is a run's worktree ({path}).\ncd to the main checkout "
                  f"and uninstall from there.", file=sys.stderr)
            return 1
    plan = build_plan(root, db, wt_dir, prefix)
    for path in [*plan["delete"], plan["worktree_dir"], *plan["worktrees"]]:
        if path == SKILL_ROOT or path in SKILL_ROOT.parents:
            print(f"refusing: this would delete the skill itself — {SKILL_ROOT} is inside "
                  f"{path}, which is on the delete list.", file=sys.stderr)
            return 1
    if not plan["delete"] and not plan["worktrees"] and not plan["gitignore"]:
        print(f"no factory here — nothing of agentic-sf's is in {root}")
        return 0

    describe(plan, root, args.branches)
    alive = live_processes(root, data_dir)
    if alive and not args.force:
        print("\nstill running — stop these first, or pass --force:", file=sys.stderr)
        for line in alive:
            print(f"  ! {line}", file=sys.stderr)
        return 1
    if alive:
        print("\n  --force: uninstalling anyway, with these alive:")
        for line in alive:
            print(f"    ! {line}")
    if args.dry_run:
        print("\n--dry-run: nothing was deleted")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("\nnothing deleted. this cannot be undone, so it asks — pass --yes to "
                  "answer ahead of time.\n(nothing to ask on: stdin is not a terminal)",
                  file=sys.stderr)
            return 1
        try:
            answer = input("\ndelete all of this? there is no undo [type 'yes']: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("nothing deleted")
            return 0

    removed = execute(plan, root, args.branches)
    print(f"\nagentic-sf uninstalled from {root}")
    for item in removed:
        print(f"  - {item}")
    if plan["keep"]:
        print("\n  left for you:")
        for note in plan["keep"]:
            print(f"    · {note}")
    left = run_branches(root, prefix)
    if left:
        print(f"\n  {len(left)} asf branch(es) still here — the record of runs that never "
              f"landed. `git branch -D` them, or re-run with --branches.")
    print(f"\nthe skill is untouched: {SKILL_ROOT}")
    print("re-stamp any time with:  uv run <skill>/scripts/install.py --harness ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
