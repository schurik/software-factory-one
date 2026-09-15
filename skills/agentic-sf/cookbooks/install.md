# Install

Stamp the factory out of the skill and into the current working directory.
Reached however this harness names it — `/agentic-sf install` in Claude Code,
`/skill:agentic-sf install` in pi, or plain words in any agent that just read
`SKILL.md`.

## Never stamp it by hand

`install.py` is the only supported way in. Copying `templates/asf/` into the
repo yourself reproduces the *files* and none of the decisions around them:

| Skipped by hand | What breaks, and when you find out |
|---|---|
| `.env` with `ASF_SKILL=` | `just uninstall` fails outright; `just up` and `just obs` start the watchers **without the trace UI**, silently. `.env` is gitignored, so it also never arrives with a clone |
| the `# agentic-sf runtime` block in `.gitignore` | `asf/data/` and `.asf-worktrees/` become tracked, and a commit stage running `git add -A` sweeps the run record into the repository |
| quality detection | every `verify` block stays a placeholder, and a placeholder **fails** with exit 78 |
| `justfile` vs `asf.justfile` | a repository's own justfile gets overwritten, or the recipes never land |
| the assembled `asf/factory.yaml` | the roster is the harness's `defaults.yaml` merged with `templates/factory.yaml`; neither half is the config on its own |

If you are looking at a repo that has `asf/` but no `.env`, that is what
happened. Re-running `install.py` from the repo root repairs it: it skips every
file that already exists and writes only what is missing.

## Ask first, stamp second

`install.py` stamps ONE set of defaults. Four of them are decisions the
repository owns, not the factory, and each is cheap to answer now and annoying
to discover later. **Put them to the engineer in one message**, with the
defaults named so they can say "all defaults" and be done.

The first has no default: **which harness**. It decides which roster, which
prompts and which `.env.sample` get stamped, so the installer asks rather than
guessing — `--harness <name>` answers ahead of time, and with no terminal to ask
at, a missing flag is an error rather than a silent choice.

| Ask | Default if they shrug | Why it is not the installer's call |
|---|---|---|
| **Which harness?** (`claude_code`, `pi`) | none — it is asked | `claude_code` runs `claude -p`, takes model *aliases* (`opus`, `sonnet`, `haiku`) and brings its own auth: **no API key at all**, which is usually the shortest path to a first green run. `pi` runs `pi -p --mode json`, takes `provider/model-id`, and needs that provider's key in `.env`. |
| **How does this repo run its tests, lint, typecheck and build?** | whatever the installer detects; **anything it cannot detect stays a placeholder, and a placeholder fails** | `install.py` reads `package.json` scripts, the lockfiles and `pyproject.toml`, and writes what it finds into `asf/engine/quality.py` marked `# detected at install`. Confirm those lines — a detected command is a guess from a filename. An unwired block exits 78 rather than passing, because a chain that reports a green suite it never ran is the most expensive default this factory could ship. |
| **How should a run's branch land?** (`worktree.integration.mode`: `merge`, `pr`, `none`) | `merge` | Repositories genuinely disagree about whether a machine may move the base branch. `merge` suits a solo repo; `pr` suits anywhere a human reviews first; `none` leaves every branch for a person. Issue- and review-triggered runs can never merge regardless — `integration` downgrades them to a pull request, in code. |
| **May issues and reviews start runs?** (`issues.enabled`, `pull_requests.enabled`) | both off | This is the one path where the prompt is written by whoever can file an issue rather than by the engineer at the keyboard. Off until the repo opts in. |

Two more worth naming only if the answer is not the default: `worktree.enabled`
(on — every run works in its own tree on branch `asf/<adw_id>`, never the
engineer's checkout) and `defaults.protected_files` (the factory's own code, so
an agent cannot edit the machinery that judges its work).

Apply the answers by editing `asf/factory.yaml` and `asf/engine/quality.py`
**after** stamping. The installer takes no flags for any of it, on purpose: the
config is the record of what this repo decided, and a flag would hide that
decision in a shell history.

## Run it

```bash
uv run <skill>/scripts/install.py --harness claude_code    # or: pi
```

`<skill>` is the directory this skill lives in — substitute the real path.

Run it from the **target repo root**: the cwd is where everything lands, and it
is never the skill's own directory. Leave `--harness` off and it asks, listing
what this skill ships; nothing is written until the question is answered, so an
abort leaves the repo untouched.

`--no-detect-quality` leaves every quality block a placeholder, for a repo whose
commands you would rather write yourself.

## What gets stamped

| Stamped | From | Tracked? |
|---|---|---|
| `asf/factory.yaml` | assembled: `templates/harnesses/<harness>/defaults.yaml` + `templates/factory.yaml` | yes — the manifest, and yours the moment it lands |
| `asf/asf.py` | `templates/asf/asf.py` | yes — the one entry point |
| `asf/engine/` | `templates/asf/engine/` | yes — session, worktree, gates, permissions, hitl, tracer, harnesses |
| `asf/stages/<name>/` | `templates/asf/stages/` | yes — the closed vocabulary: scout, plan, implement, verify, review, document, commit, integrate. Each is `stage.py` plus its default task files |
| `asf/agents/<name>/agent.md` | `templates/asf/agents/` | yes — **the user-owned home for identity**: frontmatter for the engine, prose for the model |
| `asf/workflows/<name>/` | `templates/asf/workflows/` | yes — `sdlc`, `quick`, `ship`, `issue`, `pr-review` |
| `.env.sample` | `templates/harnesses/<harness>/env.sample` | yes — only the keys that harness needs |
| `.env` | copied from `.env.sample`, with `ASF_SKILL=` written in | **no** — gitignored, and the reason a clone needs `install.py` re-run |
| `justfile`, or `asf.justfile` beside a foreign one | `templates/justfile` | yes — `just --list` is the menu |
| `.gitignore` | `+5` entries under `# agentic-sf runtime` | yes |
| `asf/data/sessions/<adw_id>/`, `asf/data/asf.db` | created at runtime | no — gitignored |
| `.asf-worktrees/` | created at runtime | no — gitignored: one worktree per run |

`asf/agents/` is yours the moment it is stamped. Edit it there, never back
inside the skill. An agent's TASK is not in it — a task belongs to the stage
that calls the agent, and a workflow may override it with its own file.

## Idempotency

Re-running is safe and doubles as a drift check. `install.py` skips **every**
file that already exists and reports the count, so a second run over a healthy
repo stamps 0 files. It still asks which harness, because the answer decides
what it would stamp into the gaps — give it the one the repo already runs, or
pass `--harness`.

`--force` refreshes stamped code (`asf/engine/`, `asf/stages/`, the shipped
workflows and agents) to the skill's current version. **It does not overwrite
`asf/factory.yaml`**: a fresh render lands beside it as `asf/factory.yaml.new`
and the installer prints `YOUR CONFIG WAS NOT TOUCHED`, leaving the diff to you.
Everything else stamped *is* replaced, including agent prose you edited, so
commit before you force.

## Post-install checklist

1. **`just doctor`** — first, always. Every check with its fix, then every
   workflow loaded and validated. It spawns nothing and costs nothing, and it is
   the designated answer to "is this repo ready to run anything?".
2. **`ASF_SKILL` in `.env`** — already written by the installer, and worth
   knowing about. Two things need it: `just uninstall` runs the uninstaller out
   of the skill, and the trace UI ships with the skill under `apps/visualizer`,
   so `just up` and `just obs` find it that way. `install.py` never overwrites a
   value that is already there; if the path came from another machine it says so
   and leaves it. Unset, `doctor` warns on both `ASF_SKILL` and `trace UI`.
3. **The harness's own steps** — `install.py` printed them after stamping, out
   of that harness's `about.md`: the CLI on PATH, how it authenticates, and the
   sharp edges (`safe_mode`, running as root, how a model id is resolved).
   Re-read them there rather than guessing which apply.
4. **The quality blocks** — `doctor` names every block still unwired. Write the
   real argv into `asf/engine/quality.py` as a **list**, calling binaries by
   bare name. A `verify` stage that names an unwired block fails the run.
5. **`just list`** — the workflows, one line each. Then a first run:
   [run_workflow.md](run_workflow.md).

## If the skill is vendored inside the repo

Some repos keep the skill in-tree (`.agents/skills/agentic-sf/`) rather than
pointing `ASF_SKILL` at a checkout elsewhere. That works, with one thing to
know: `just up` runs `bun install` in `<skill>/apps/visualizer` on first use,
which writes `node_modules/` inside the tracked skill tree. The skill ships
`apps/visualizer/.gitignore` to cover it, so it stays out of the host repo's
`git status` — and out of the `git add -A` a commit stage runs.

## Turning on issue- and review-triggered runs

Off by default, and a separate decision from installing. `issues.enabled`,
`issues.route` and `pull_requests.enabled` in `asf/factory.yaml`, then
`just issues-status` and `just prs-status` to see what the watchers would do and
whether they can. `just up` runs both watchers and the trace UI in one process.

## Removing it again

[uninstall.md](uninstall.md). The skill is never touched by either direction.
