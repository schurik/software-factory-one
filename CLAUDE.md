# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This repo is **not an application and not an installable package**. It is the source of two
*agent skills* — `skills/sssf` and `skills/agentic-sf` — each of which stamps a deterministic
Python control plane ("a factory") into *someone else's* repository.

Consequences that shape every change here:

- `skills/*/templates/` is **exactly what `install.py` copies into a target repo**. Code under
  `templates/` is never imported from here in production; it runs as `adws/` (sssf) or `asf/`
  (agentic-sf) in the target repo, with paths relative to *that* repo's root.
- The tests import straight out of the template directories (see `skills/agentic-sf/tests/conftest.py`)
  and install into a real `git init`'d `tmp_path`. So a fix goes in `templates/`, never in a
  stamped copy.
- `skills/*/SKILL.md` + `cookbooks/` + `references/` are **read by an agent at runtime**. They are
  product surface, not documentation about the product — editing behaviour usually means editing
  both the Python and the SKILL.md routing/rules that describe it.

`README.md` is the long-form explanation of the design; `docs/` is the fork's phase roadmap
(phases 1, 2, 5, 6, 7, 9 built; 3, 4, 8 not started).

## Commands

```bash
pytest                                  # both suites; testpaths + addopts in pyproject.toml
pytest skills/sssf/tests/test_gates.py                       # one file
pytest skills/agentic-sf/tests/test_asf_e2e.py::test_name    # one test
pytest -k permissions                                        # by name
ruff check .                            # line-length 100; apps/ (TypeScript) excluded
```

Nothing in the suite calls a model, opens a socket, or needs a token — the `fake` harness answers
from a script. `filterwarnings = ["error::DeprecationWarning"]` is deliberate: a dependency release
can turn CI red with no PR change, and the fix belongs in the module the traceback names.

Manual smoke test — stamping into a scratch repo is what CI does and the only thing that proves the
PEP-723 headers resolve (pytest invokes installers with `sys.executable`, which ignores them):

```bash
mkdir /tmp/scratch && cd /tmp/scratch && git init -q . && git commit -q --allow-empty -m init
uv run /path/to/software-factory/skills/agentic-sf/scripts/install.py --harness claude_code
uv run asf/asf.py list && uv run asf/asf.py check      # loads every workflow, spawns nothing
uv run /path/to/software-factory/skills/agentic-sf/scripts/uninstall.py --dry-run
```

The visualizers (`skills/*/apps/visualizer`, Vue + Vite on Bun) have their own scripts:
`bun run typecheck`, `bun run lint` (oxlint), `bun run build`. CI does not run them.

## The two factories

**`skills/sssf`** — the original. A workflow is a **Python script** (`adws/adw_*.py`, 40–180 lines)
that composes phases by hand; all low-level logic lives in `adws/adw_modules/`. Roster in one file,
`adws/adw_sssf_config/sssf.config.yaml`. Twelve starter ADWs.

**`skills/agentic-sf`** — the second factory, standing alone (does not import sssf). A workflow is a
**directory** (`asf/workflows/<name>/workflow.yaml` + optional `tasks/*.md` + agent bindings) built
from a **closed stage vocabulary** (`asf/stages/<name>/stage.py`); one entry point, `asf/asf.py`
(`list | check | run | doctor | resume | kill | up | status | …`). An agent is one `agent.md`:
frontmatter for the engine, prose for the model. Manifest is `asf/factory.yaml` (no agents in it).

Both share the same engine shape — `session`, `worktree`, `gates`, `permissions`, `limits`, `hitl`,
`tracer`, `harnesses/{pi,claude_code,fake}` — so a fix to one is usually worth porting to the other,
but they are separate trees on purpose and diverge where the design diverges.

## Invariants the code (and the tests) enforce

These are load-bearing. Breaking one is a red suite, and in most cases the test exists because the
bug already shipped once.

1. **Code owns sequencing, retries and acceptance; an agent owns one bounded phase.** A known
   invocation (`bun test`, `ruff check`) is a `kind="code"` phase via `quality.py`, never an agent.
2. **The factory never reads the trace db.** `tracer.py` writes SQLite; every question about a
   session is answered from that session's own directory under `data_dir`. `test_no_db_reads.py`
   asserts this structurally — a run must still work with the db deleted. Tests may read it to assert.
3. **Typed envelopes only, and the contract is a synced triad**: the `EnvelopeBase` subclass in
   `data_types.py`, the JSON example in the agent's task/`user.md` `## Report` block, and
   `output_type=` at the call site. Change one, change all three in the same edit.
4. **Gates verify claims after the fact**, against the envelope's own declarations — never predictions.
   A failed gate or unparseable JSON re-prompts the *same* session as a correction; nothing restarts.
5. **`tools:` is a capability list, `writes:` is the boundary.** `permissions.py` diffs the repo after
   every agent call and rolls back unauthorized changes. In agentic-sf, workflow bindings may only
   *narrow* `writes`/`tools`, and identity is *appended* (`system_append`), never replaced.
6. **Every phase carries a real description** (a restatement of the name is rejected at construction),
   and **every run ends through `run.finish(accepted=)`** — phases passing ≠ the run being acceptable.
7. **agentic-sf's vocabulary is closed**: no `loop:` or `if:` in `workflow.yaml`. A shape the
   vocabulary cannot express is a new stage in Python. `check` refuses a workflow before it costs
   anything (unknown stage/option, missing agent, drifted report block, widened binding).
8. Functions over four parameters take one concrete type instead (`AgentCall`, `PhaseParams`).

## Working in here

- **Prompts are stamped per harness.** `templates/harnesses/{pi,claude_code}/` each carry their own
  roster, defaults, `env.sample` and prompt set — pi's prompts reference `subagent_*` tools Claude
  Code does not have. Moving an agent between harnesses means moving its prose too; validation only
  catches the `tools:` half. Adding a harness = one module in `harnesses/` + one template directory.
- **The `fake` harness is the development tool.** Put a new chain's roster on it until the shape
  holds; it is never offered by the installer.
- **Installers are idempotent.** A second run skips what exists and reports it (a drift check).
  `--force` overwrites *everything* stamped, config and prompts included.
- **Runtime must stay gitignored.** CI fails the install if `adws/adw_data/sessions/`, `asf/data/`,
  `.sssf-worktrees/`, `.asf-worktrees/` or `.pyc` files end up staged — a chain's commit phase runs
  `git add -A` in the user's repo.
- Scripts carry `#!/usr/bin/env -S uv run` + PEP-723 deps (`pydantic`, `python-dotenv`, `pyyaml`,
  `rich`); nothing is pinned, so CI meets the versions a stamped repo would resolve that day.
- Commit subjects follow `<area>: <what changed, in the imperative, lowercase>` — e.g.
  `issues: the four state labels are mutually exclusive, and now stay that way`.
- When a claim in this repo is worth making, it is usually made in prose *next to the code* (see the
  comment headers in `pyproject.toml`, `ci.yml`, `stage.py`). Match that register rather than
  stripping it.
