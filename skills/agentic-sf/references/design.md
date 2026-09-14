# Design: workflows as directories over a closed vocabulary

What agentic-sf is, the rules that hold it together, and why each exists.

## The problem it answers

The obvious shape for an agent factory is one script per workflow, and it
asks the engineer, on day one, which of a dozen. "Copy the closest and edit
the phase list" does not work, because a hundred-line chain is not a list:
its value is the fix loop, the retest-only-if-revised, the commit-after-green
— the wiring *between* phases. And an agent's behaviour for a given task ends
up spread over many places (a roster entry, a system prompt, a user prompt,
the output type, the call site, prose constants in Python, gates in config),
because a per-agent user prompt is the wrong owner when the task is per
stage.

## What Warp got right, and what to take

Warp Factories define a factory as `factory.yaml` plus an agents directory,
and route work through a *fixed* set of stages with a foreman agent choosing
the path. The manifest is inventory and policy, never a pipeline. asf takes
the manifest and the fixed vocabulary; it does not take the in-run foreman.
The stage sequence stays deterministic code, chosen before the run by a
person or the orchestrator session. Agent proposes, code disposes.

## Three layers, three owners

| Layer | Where | Owns | Edited |
|---|---|---|---|
| A · Roster | `asf/agents/<name>/` | identity, model, tools, `writes` — the security boundary | rarely, reviewed |
| B · Stages | `asf/stages/<name>/` | the contract, loops, conditions, default task files | when the vocabulary grows |
| C · Workflows | `asf/workflows/<name>/` | which stages, options, agent bindings, task overrides | often |

### Agents

```
asf/agents/planner/
  agent.md        frontmatter: purpose, thinking, color, tools, writes   (name = directory)
                  body: who the agent is
  agent.pi.md     optional: the identity for one harness, when it must differ — prose only
```

One file, two readers. `engine.factory` parses the frontmatter and enforces
it; `engine.prompts` strips it and hands the model the body. The same shape
as a Claude Code subagent file. No `user.md`: the task belongs to the stage.

### Stages

A stage module declares `NAME, KIND, OUTPUT, NEEDS, TASKS, Options, run` and
optionally `check`. `Options` is a pydantic model with `extra="forbid"`.
`NEEDS` is what must precede it; `OUTPUT` is what it hands on, or `None` to
pass the previous envelope through. `TASKS` maps a key to `(default file,
envelope type the agent answers with)`; the two can differ from `OUTPUT` —
`review` asks its reviewer for a `ReviewOutput` and hands on the
`BuildOutput` as revised. A workflow overrides a task with `tasks/<key>.md`,
and the report check runs against the task's own type.

The loader walks the stage list with a "current envelope type" and refuses a
stage whose `NEEDS` the chain does not satisfy. `NEEDS = ()` means "anything
or nothing": `plan` takes a `ScoutOutput` as its `previous` when a `scout`
runs first, and its task tells the planner to read the findings as recon,
not as a plan. `check(opts, earlier)` lets a
stage add its own static rule: `commit` requires `of:` to name an earlier
stage whose output carries `commit_message`.

`ctx.current(stage_name)` returns that stage's work product *as it stands
now*: after `verify`, the build is the fixed build. `commit: {of: implement}` lands
that one.

A stage that must end the run without failing its phase raises `StageStop`.
The runner finishes the run as not accepted, with the reason, and nothing after
it runs.

### Workflows

```yaml
name: ship
description: one line — it is what `list` shows
input: prompt
agents:
  planner: {from: planner, model: opus, system_append: [agents/planner.md]}
  fixer:   {from: builder, thinking: high, writes: [src/]}
stages:
  - plan:   {agent: planner, hitl: true}
  - commit: {of: plan}
  - implement: {agent: builder}
  - verify: {blocks: [test, lint], max_fix_loops: 3, fix: {agent: fixer}}
  - commit: {of: implement}
```

`input:` is where the request comes from, and it is the runner's business,
not a stage's: `prompt` (the default) records the text; `issue` reads the
work item before the first stage and comments the outcome after the last;
`pr` reads the pull request BEFORE a session exists — its branch names the
session to join — and answers the threads after the last stage. The
stranger's text reaches the first stage as `ctx.previous`, an envelope whose
artifact is the text with its framing, and the operator's instruction is
`ctx.prompt`. So `issue` is `ship` with `input: issue`, and `pr-review` is
implement → verify → commit with a task override and `allow_clean: true` on
the commit — the option that gives a review run its third outcome
(`declined`: an accepted run whose tree the builder did not touch, on
purpose). Nothing in a workflow file can make an issue-triggered run merge;
`integration` downgrades it to a pull request in code.

Rules, enforced at load:

1. **Vocabulary is closed.** A stage name must be a directory under
   `asf/stages/`. No control flow keys exist.
2. **Bindings narrow.** `writes` and `tools` in a binding must be covered by
   the roster's; `None` in the roster (unrestricted) is narrowed by anything.
   `harness` and `harness_options` cannot be bound at all.
3. **Identity appends.** `system_append` is a list of files under the workflow
   directory; `system` is not a binding key.
4. **Tasks are checked against types.** The `## Report` JSON block of every
   task file is compared with the stage's `OUTPUT` model: no unknown keys, no
   missing required ones, and the three placeholders present.
5. **Gates layer.** `--hitl` on the command line, then the stage's `hitl:`
   option, then factory.yaml's `hitl:` block.

## The engine

`asf/engine/` is the run machinery: session, worktree, permissions, gates,
replay, hitl, limits, the tracer, the harnesses. The workflow layer sits on
top of it through a small seam:

- `PromptEngineering.user` is optional and `system_append` exists: an
  identity is the roster's file plus what a workflow appends.
- `AgentCall.task` and `AgentCall.variables`: the user prompt per call, which
  is how a stage's task file reaches the agent.
- `session.ensure(..., name=)`: the trace and `run.json` name the workflow.
- `quality.run_blocks(run, names)`: a verify stage picks its blocks.
- `agents.merge_defaults(raw)`: one merge over defaults, used by `engine.factory`.

And five modules of its own: `stage.py` (contract and registry), `tasks.py`
(resolution and the report check), `factory.py` (roster from directories),
`workflow.py` (load, validate, run), `inputs.py` (where a request comes from
and where its outcome goes). The trace db is `asf/data/asf.db`; the
visualizer under `apps/visualizer` reads it.

## What this costs

- Readability moves. A script would explain a run top to bottom; now
  `workflow.yaml` plus the stage modules do. The trace shows the sequence
  either way.
- The vocabulary will want to grow. A new *stage* is Python with a contract;
  a new *option* is policy on an existing stage; anything else is a
  `workflow.py` escape hatch (not built — nothing has needed it).

## Slices

1. **Done:** plan, implement, verify, commit; `sdlc` and `quick`; loader, runner,
   installer; fake-harness e2e and layout tests.
2. **Done:** review (with revise and retest), document, integrate stages;
   scout, ahead of the planner in `ship`; the gate CLI (`pending`, `show`, `approve`, `reject`, `abort`,
   `resume`) in `engine/operate.py`; `doctor`; the justfile.
3. **Done:** `input: issue` and `input: pr` (`engine/inputs.py`), the `issue`
   and `pr-review` workflows, both watchers (`engine/watch.py`), `up` and
   `status` (`engine/supervise.py`), `kill` and the worktree verbs, the label a
   re-entered issue run lands on resume, uninstall. The `workflow.py` escape
   hatch was planned for pr-review and turned out unnecessary: one option on
   `commit` covered it. It stays unbuilt until a shape actually needs it.
