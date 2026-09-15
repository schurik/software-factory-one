# Uninstall

Delete the stamped factory out of a repository. **The skill is untouched** — it
keeps every generator and can stamp the factory back tomorrow.

```bash
just uninstall --dry-run      # always this first
just uninstall                # then this
```

`just uninstall` runs `<skill>/scripts/uninstall.py` out of `ASF_SKILL`; without
that set the recipe fails with a message saying so. The direct form is
`uv run <skill>/scripts/uninstall.py [--dry-run] [--yes] [--branches] [--force]`,
from the **target repo root** — the cwd is what gets emptied.

## Say what dies, before you run it

This is the one irreversible thing in the factory, and most of what it deletes
is not recoverable from git: the run record under `asf/data/` was never tracked,
and neither were the worktrees. So do not run it on an engineer's say-so alone.

**Run `--dry-run`, show them the plan, and let them answer.** It prints exactly
what would be deleted, what would be kept and why, how many worktrees go, and
which branches would survive. Nothing is written.

Three questions worth putting to them, because the answers are in the plan and
not in their head:

- **Any run that never landed?** Its branch is the only copy. `--branches` also
  deletes the `asf/<adw_id>` branches; without it they all survive the uninstall
  and you can read them afterwards.
- **Anything in a kept worktree?** A failed run keeps its worktree on purpose —
  it is where you go to see what happened.
- **The trace db?** It goes with the run record. Every question about past runs
  goes with it.

## What it removes

| Removed | Note |
|---|---|
| `asf/` entire | the engine, the stages, **and the workflows and agents you own**, plus the whole run record under `asf/data/` |
| the trace db | under `asf/data/` it goes with the directory; pointed elsewhere by `observability.db`, it is deleted by name along with its `-wal`/`-shm` siblings |
| every run worktree | git metadata included, so git does not keep believing in them |
| `.env.sample` | only when it is one this skill ships today |
| the stamped `justfile` (or `asf.justfile`) | only when it still matches what was stamped |
| the `# agentic-sf runtime` block in `.gitignore` | the exact lines the installer added |
| `.env` | deleted **only** when it holds nothing but the sample's own content plus the `ASF_SKILL=` line. Hold anything else and the file is kept, with just that line removed |

## What it keeps, and why

Three files live in a namespace the repository had before the factory arrived —
`justfile`, `.env`, `.gitignore` — so each is compared against what was stamped
and **kept the moment it differs**. A `.env` holding your API keys and a justfile
holding your own recipes are not the factory's to delete. Each kept file is
printed with the reason and what to do about it:

- **`.env`** — kept whenever it holds anything the sample does not, and then
  only the `ASF_SKILL=` line goes. A `.env` that is still just the sample plus
  that line is deleted whole, because nothing of yours is in it.
- **`justfile` / `asf.justfile`** — kept when it is this repo's own, or a stamped
  one that has since diverged (your recipes, or an older version of the skill).
- **`.env.sample`** — kept when it is not one this skill ships today: either
  yours, or from an older version. Read it, then delete it yourself.
- **Files under `asf/` that were never stamped** — anything you added is listed
  before it goes, because `asf/` is deleted whole.
- **Branches** — `asf/<adw_id>` branches are never deleted without `--branches`.

## It refuses rather than half-finish

- **While anything is running.** A half-deleted factory under a live run is the
  one state worse than either end of this. It lists each live run and watcher
  with the command that stops it — `asf kill <adw_id>`, or ctrl-c on the
  terminal that owns `just up` — and `--force` says you have.
- **Inside the skill**, or when the cwd is a run's worktree rather than the main
  checkout, or when the uninstall would delete the skill itself.

It imports nothing from `asf/` and uses only the standard library, so it still
works on a factory that is already broken — which is when it is most wanted.

## Afterwards

`git status` shows the deletions. Commit them, or `git checkout` the tracked
parts back to undo everything except the run record, which is gone either way.
To stamp it again: [install.md](install.md).
