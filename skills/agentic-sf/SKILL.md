---
name: agentic-sf
description: Agentic Software Factory — workflows as directories (workflow.yaml + tasks + agent bindings) over a closed stage vocabulary, with one entry point. Use when the user asks to install agentic-sf (`/agentic-sf install`, or plain words), run a workflow with asf, list or check workflows, create or edit a workflow directory, tune an agent's identity or a stage's task, or inspect a run's trace. Keywords - agentic-sf, asf, software factory, workflow.yaml, stage, task file, agent directory, factory.yaml.
argument-hint: "[install | run <workflow> \"<prompt>\" | list | check | create workflow | edit agent | ...]"
---

# Agentic Software Factory (asf)

Deterministic code owns sequencing, retries and acceptance; coding agents
work inside bounded phases; typed envelopes cross the seams; everything
streams into SQLite. The surface:

- **One entry point.** `just do "<prompt>"`, or `uv run asf/asf.py run <workflow>
  "<prompt>"`. `list`, `check`, `doctor`, the gate verbs, the watchers, `up`,
  `status`, `kill` and `worktrees` beside it.
- **A workflow is a directory**, not a script you copy. `workflow.yaml` names
  stages from a closed vocabulary and gives each its options. Loops and
  conditions live in the stages; the YAML gets numbers.
- **An agent is one file**, `agent.md`: YAML frontmatter for the engine, prose
  for the model. Its task is not
  there: a task belongs to the stage that calls the agent, and a workflow may
  override it with its own file.

## `<skill>` in every command below

The directory this `SKILL.md` lives in. Substitute it, never the literal
`<skill>`. Commands run from the **target repo root**.

## Startup

Two steps. Then stop.

1. If `asf/factory.yaml` does not exist, say in one line that the factory is
   not installed here and offer [cookbooks/install.md](cookbooks/install.md).
   Stamping it by hand instead of running `install.py` is how a repo ends up
   with `asf/` but no `.env` — read the cookbook. Otherwise:
2. Run `just list` (or `uv run asf/asf.py list`) and print it — one line per
   workflow, name and description — and **wait for the engineer's request.**

Nothing else. No trace-db queries, no reading the config, no reading stages
or engine code, no "current state" summary. Everything below is lazy-loaded
when a request calls for it.

Exception: if the engineer's first message already contains a request, skip
the waiting and route it.

## Orchestrator rules

You run the system and help the engineer interact with it. **You do no
workflow work yourself**: never plan, implement or test in an agent's place —
launch the workflow and watch it. Never edit files under `asf/data/`; that is
the run record. The trace db (`asf/data/asf.db`) is yours to query when
observing is the task, never to volunteer a status board.

## Where things live in a stamped repo

```
asf/
  factory.yaml            the manifest: defaults, budget, gates, trace, worktree. No agents in it.
  asf.py                  the runner: list | check | run
  agents/<name>/          agent.md: frontmatter (model, thinking, tools, writes, purpose) + identity below it
  workflows/<name>/       workflow.yaml (input, agents, stages), optional tasks/<key>.md, optional agents/<x>.md
  stages/<name>/          stage.py (the contract) + its default task files
  engine/                 the machinery: session, worktree, gates, permissions, hitl, tracer, …
  data/                   runtime: sessions/<adw_id>/ — never edit
```

Every run works in its own worktree on branch `asf/<adw_id>`; the checkout is
never touched. A run that is not accepted keeps its worktree. A gate a stage
turns on with `hitl: true` stops the run with exit 75 and the session reading
`waiting`. An issue- or review-triggered run can never merge: `integration`
downgrades a configured merge to a pull request on those inputs, in code.

## Request routing

Commands are inline; three rows carry a cookbook as well, and those are the
three whose answer is a decision process rather than a command. Read it before
acting, not after.

| Request | Do |
|---|---|
| install / set up the factory here | [cookbooks/install.md](cookbooks/install.md) — **read it first**: four decisions belong to the repo, and `install.py` is the only supported way in. Then `uv run <skill>/scripts/install.py --harness claude_code\|pi` and `just doctor` |
| "is this repo ready to run?" / something failed before the first phase | `just doctor` — every check with its fix, then every workflow checked; spawns nothing. [cookbooks/install.md](cookbooks/install.md#post-install-checklist) |
| run a workflow | `just do "<prompt>"` (sdlc), `just quick`, `just ship`, or `just run <name> "<prompt>" [--hitl all\|none\|plan]` — turning the request into a prompt, watching it, gates, failures: [cookbooks/run_workflow.md](cookbooks/run_workflow.md) |
| work a tracked issue / answer a review | `just issue 42`, `just pr-review 17` — the number, never a prompt; the run reports back on the issue or in the threads |
| start the watchers / "is anything polling?" | `just up` (both watchers + trace UI, ctrl-c stops all), `just status`; one poll: `just issues`, `just prs`; cron form: `just issues-watch`, `just prs-watch`. Turn them on in `factory.yaml` (`issues.enabled`, `issues.route`, `pull_requests.enabled`) |
| stop a run | `just kill <id>` — agents first, then the workflow; `--force` SIGKILLs |
| tidy up | `just worktrees`, `just worktrees-prune [--force]`, `just worktrees-remove <id>`; branches are never deleted |
| remove the factory from this repo | [cookbooks/uninstall.md](cookbooks/uninstall.md) — `just uninstall --dry-run` first and show the plan; the skill is untouched, the run record goes with `asf/`, and it is the one irreversible thing here |
| which workflows exist / what does X do | `uv run asf/asf.py list`; read `asf/workflows/<name>/workflow.yaml` |
| is this workflow runnable | `uv run asf/asf.py check <name>` — spawns nothing, names every problem |
| create a workflow | copy the closest directory under `asf/workflows/`, edit `workflow.yaml`, run `check`. Read [references/design.md](references/design.md#workflows) first |
| tailor an agent's TASK for one workflow | add `asf/workflows/<name>/tasks/<key>.md` — keys are the stage's TASKS (scout, plan, implement, fix, review, revise, document). Keep the `## Report` block matching the type; `check` verifies it. `pr-review/tasks/implement.md` is the shipped example |
| tailor an agent's IDENTITY for one workflow | bind it in `workflow.yaml` under `agents:` with `system_append: [agents/<x>.md]` — append, never replace |
| change an agent for every workflow | edit `asf/agents/<name>/agent.md` — the frontmatter is the boundary, the prose is the voice |
| add a stage to the vocabulary | a directory under `asf/stages/` meeting the contract in `asf/engine/stage.py`; [references/design.md](references/design.md#stages) |
| pick a failed run back up | `just resume <id>` — replays recorded agent phases, re-runs what code owns |
| a run is waiting at a gate / "why is this run waiting?" | `just pending`, `just show <id>`, then `just approve <id> [-m]`, `just reject <id> -m "..."` or `just abort <id>`. Never approve on the engineer's behalf |
| watch a run | `just sessions`, `just phases <id>`, `just tail <id>`; `just obs` boots the trace UI (`apps/visualizer` in the skill, needs bun) |

## Hard rules

1. **A workflow is refused before it costs anything.** `check` and `run` load
   the whole directory first: unknown stage, unknown option, a `verify` with no
   build before it, an agent the roster lacks, a task whose report block
   drifted from the envelope type, a binding that widens `writes` or `tools`.
2. **The vocabulary is closed.** No `loop:` or `if:` in workflow.yaml, ever. A
   shape the vocabulary cannot express is a new stage in Python, or a
   `workflow.py` escape hatch (not built — nothing has needed one).
3. **Bindings narrow, never widen.** The roster is the security boundary; a
   workflow is edited often.
4. **Identity is appended, never replaced.** Five workflows must not become
   five builders.
5. **Tasks carry the words, stages carry the facts.** A stage passes
   `{{variables}}`; no prose lives in Python.
6. **The engine's rules hold under every workflow.** An agent answers with a
   typed envelope or the phase fails; gates verify its claims against the
   tree; `writes:` and `protected_files` are enforced in code after every
   call; every phase carries a description; a run ends through
   `run.finish(accepted=)` and nowhere else.

## What is here, and what is not

Three slices: the eight stages; `sdlc`, `quick`, `ship`, `issue` and
`pr-review`; the loader and runner with the three inputs; the gate CLI;
doctor; both watchers, `up`, `status`, `kill`, the worktree verbs; the
justfile; uninstall. Not ported: a `workflow.py` escape hatch — nothing has
needed one yet, `pr-review` fit the vocabulary with one option. The trace
UI ships with the skill under `apps/visualizer`; `just up` and `just obs`
find it through the `ASF_SKILL` the installer writes into `.env`.
