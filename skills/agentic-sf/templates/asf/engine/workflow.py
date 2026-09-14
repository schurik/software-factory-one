"""A workflow directory, loaded, checked, and run.

    asf/workflows/<name>/
        workflow.yaml     which stages, with which options, played by which agents
        tasks/<key>.md    optional: a task file that overrides the stage's default
        agents/<x>.md     optional: text a binding appends to an agent's identity

Everything a workflow.yaml can get wrong is found here, before a session
exists: a stage that is not in the vocabulary, an option no stage takes, a
`verify` with no implement before it, an agent the roster does not have, a task
whose report block drifted from the envelope type, a binding that tries to
widen what an agent may write. `asf.py check` is this function and nothing
else, and `asf.py run` calls it first.

The rules a binding lives by, and why:

  * `writes` and `tools` may only NARROW the roster's. The roster is reviewed
    once and is the security boundary; a workflow is edited often. If a
    workflow could widen either, the file people touch most would be the one
    where an agent gains write access.
  * `system_append` is allowed, `system` is not. Identity stays shared, or
    five workflows drift into five builders and the roster means nothing. An
    agent that really is different is a new directory under asf/agents/.
  * Loops and conditions live in stages, never here. A workflow gets numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import agents, factory, git_helper, inputs, session, tasks
from .data_types import AgentConfig, BuildOutput, EnvelopeBase, PhaseParams, FactoryConfig
from .stage import StageContext, StageModule, StageStop, Step, load_registry


class Binding(BaseModel):
    """`agents: {alias: {...}}` — a roster agent as this workflow plays it."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    model: Optional[str] = None
    thinking: Optional[str] = None
    timeout_seconds: Optional[int] = None
    writes: Optional[list[str]] = None
    tools: Optional[list[str]] = None
    system_append: list[str] = Field(default_factory=list)   # paths relative to the workflow dir


class Spec(BaseModel):
    """workflow.yaml, as written."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input: Literal["prompt", "issue", "pr"] = "prompt"
    agents: dict[str, Binding] = Field(default_factory=dict)
    stages: list[dict[str, Any]]


@dataclass
class Workflow:
    name: str
    description: str
    directory: Path
    cfg: FactoryConfig
    steps: list[Step]
    required_agents: list[str] = field(default_factory=list)
    input: str = "prompt"                  # prompt | issue | pr — see engine.inputs


def workflows_dir(config_path: str | Path) -> Path:
    return factory.root_of(config_path) / "workflows"


def available(config_path: str | Path = factory.DEFAULT_CONFIG) -> list[tuple[str, str]]:
    """(name, description) for every workflow directory, read without loading."""
    found = []
    for directory in sorted(p for p in workflows_dir(config_path).iterdir() if p.is_dir()):
        spec_path = directory / "workflow.yaml"
        if not spec_path.is_file():
            continue
        raw = yaml.safe_load(spec_path.read_text()) or {}
        found.append((directory.name, str(raw.get("description", "")).strip()))
    return found


def load(name: str, config_path: str | Path = factory.DEFAULT_CONFIG) -> Workflow:
    """Load one workflow and refuse it on the first inconsistency."""
    cfg = factory.load(config_path)
    root = factory.root_of(config_path)
    directory = workflows_dir(config_path) / name
    spec_path = directory / "workflow.yaml"
    if not spec_path.is_file():
        names = ", ".join(n for n, _ in available(config_path)) or "(none)"
        raise SystemExit(f"no workflow {name!r} — {directory} has no workflow.yaml. "
                         f"Available: {names}")
    try:
        spec = Spec(**(yaml.safe_load(spec_path.read_text()) or {}))
    except ValidationError as error:
        raise SystemExit(f"{spec_path}: {_flat(error)}") from None
    problems: list[str] = []
    if spec.name != name:
        problems.append(f"name is {spec.name!r} but the directory is {name!r}")
    if not spec.description.strip():
        problems.append("description is empty — it is the one line the orchestrator shows")

    cfg = _bind_agents(cfg, spec, directory, problems)
    registry = load_registry(root / "stages")
    steps = _steps(spec, registry, cfg, directory, problems)

    if problems:
        raise SystemExit(f"workflow {name!r} ({spec_path}) is not runnable:\n- "
                         + "\n- ".join(problems))
    required = sorted({a for step in steps for a in _agent_fields(step.opts)})
    return Workflow(name=name, description=spec.description.strip(), directory=directory,
                    cfg=cfg, steps=steps, required_agents=required, input=spec.input)


# ── agents ───────────────────────────────────────────────────────────────────

def _bind_agents(cfg: FactoryConfig, spec: Spec, directory: Path,
                 problems: list[str]) -> FactoryConfig:
    by_name = {agent.name: agent for agent in cfg.agents}
    bound: dict[str, AgentConfig] = dict(by_name)
    for alias, binding in spec.agents.items():
        base = by_name.get(binding.from_)
        if base is None:
            problems.append(f"agents.{alias}: from {binding.from_!r} is not in the roster "
                            f"({', '.join(sorted(by_name)) or 'empty'})")
            continue
        update: dict[str, Any] = {"name": alias}
        for key in ("model", "thinking", "timeout_seconds"):
            value = getattr(binding, key)
            if value is not None:
                update[key] = value
        if binding.tools is not None:
            widened = _widens(binding.tools, base.tools, exact=True)
            if widened:
                problems.append(f"agents.{alias}: tools {widened} are not in {base.name}'s "
                                f"roster list — a workflow may narrow tools, never widen them")
            update["tools"] = binding.tools
        if binding.writes is not None:
            widened = _widens(binding.writes, base.writes, exact=False)
            if widened:
                problems.append(f"agents.{alias}: writes {widened} are not covered by "
                                f"{base.name}'s roster writes {base.writes} — a workflow may "
                                f"narrow the boundary, never widen it")
            update["writes"] = binding.writes
        appends = []
        for ref in binding.system_append:
            path = directory / ref
            if not path.is_file():
                problems.append(f"agents.{alias}: system_append {ref} not found under {directory}")
            appends.append(str(path))
        update["prompt_engineering"] = base.prompt_engineering.model_copy(update={
            "system_append": [*base.prompt_engineering.system_append, *appends]})
        bound[alias] = base.model_copy(update=update)
    return cfg.model_copy(update={"agents": list(bound.values())})


def _widens(requested: list[str], allowed: Optional[list[str]], exact: bool) -> list[str]:
    """Entries of `requested` the roster's `allowed` does not already cover.
    `None` in the roster means unrestricted, which anything narrows."""
    if allowed is None:
        return []
    if exact:
        return [item for item in requested if item not in allowed]
    return [item for item in requested
            if not any(item == rule or (rule.endswith("/") and item.startswith(rule))
                       for rule in allowed)]


# ── stages ───────────────────────────────────────────────────────────────────

def _steps(spec: Spec, registry: dict[str, StageModule], cfg: FactoryConfig,
           directory: Path, problems: list[str]) -> list[Step]:
    steps: list[Step] = []
    known_agents = {agent.name for agent in cfg.agents}
    current: Optional[type[EnvelopeBase]] = None
    earlier: dict[str, Optional[type]] = {}
    if not spec.stages:
        problems.append("stages is empty")
    for index, item in enumerate(spec.stages, start=1):
        if not isinstance(item, dict) or len(item) != 1:
            problems.append(f"stages[{index}]: each entry is one `- <stage>: {{options}}`")
            continue
        (stage_name, raw_opts), = item.items()
        stage = registry.get(stage_name)
        if stage is None:
            problems.append(f"stages[{index}]: {stage_name!r} is not a stage — the vocabulary "
                            f"is {', '.join(sorted(registry))}")
            continue
        try:
            opts = stage.options(**(raw_opts or {}))
        except ValidationError as error:
            problems.append(f"stages[{index}] {stage_name}: {_flat(error)}")
            continue
        for agent_name in _agent_fields(opts):
            if agent_name not in known_agents:
                problems.append(f"stages[{index}] {stage_name}: agent {agent_name!r} is neither "
                                f"in the roster nor bound under agents: "
                                f"({', '.join(sorted(known_agents))})")
        if stage.needs and (current is None or not issubclass(current, stage.needs)):
            wanted = " | ".join(t.__name__ for t in stage.needs)
            got = current.__name__ if current else "nothing"
            problems.append(f"stages[{index}] {stage_name}: needs a {wanted} from an earlier "
                            f"stage, but what precedes it hands on {got}")
        problems += [f"stages[{index}] {stage_name}: {p}" for p in stage.check(opts, earlier)]
        step = Step(stage=stage, opts=opts)
        for key, (filename, answer_type) in stage.tasks.items():
            path = tasks.resolve(key, stage.directory / filename, directory)
            step.tasks[key] = str(path)
            problems += [f"stages[{index}] {stage_name}: {p}"
                         for p in tasks.check(path, answer_type)]
        _merge_hitl(cfg, stage, opts)
        steps.append(step)
        earlier[stage_name] = stage.output
        if stage.output is not None:
            current = stage.output
    return steps


def _agent_fields(opts: BaseModel) -> list[str]:
    """Every `agent:` an options model names, however deep — the stage's fix
    loop nests one under `fix:`."""
    found = []
    for name, value in opts:
        if name == "agent" and isinstance(value, str):
            found.append(value)
        elif isinstance(value, BaseModel):
            found += _agent_fields(value)
    return found


def _merge_hitl(cfg: FactoryConfig, stage: StageModule, opts: BaseModel) -> None:
    """A stage's `hitl:` option decides its gate for this workflow. Three
    layers, each overriding the one below: `--hitl` on the command line, this,
    then factory.yaml's `hitl:` block. `None` here means the workflow has no
    opinion and factory.yaml decides."""
    wanted = getattr(opts, "hitl", None)
    if wanted is None:
        return
    cfg.hitl.gates[stage.name] = "on" if wanted else "off"


def _flat(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in error.errors())


# ── running ──────────────────────────────────────────────────────────────────

def run(workflow: Workflow, request: str, adw_id: Optional[str] = None,
        resume: bool = False, hitl: str = "") -> int:
    """Play the workflow's stages in order against one session. Returns the
    exit code `run.finish` decided.

    `request` is what `input:` says it is: the prompt, or an issue or pull
    request number. The input is opened before the first stage and reported
    to after the last; the stages see only `ctx.prompt` and `ctx.previous`.
    """
    agents.validate(workflow.cfg, workflow.required_agents)
    cfg = workflow.cfg
    context, number = None, 0
    if workflow.input != "prompt":
        number = inputs.number_of(request, workflow.input)      # before a session exists
    if workflow.input == "pr":
        # The pull request names its own session, and that is decided before
        # one exists: a refusal costs one forge call and leaves nothing behind.
        adw_id, context = inputs.locate_pr(cfg, number, adw_id)
    run = session.ensure(cfg, adw_id, resume, hitl, name=workflow.name)

    if workflow.input == "issue":
        opened = inputs.open_issue(run, cfg, number)
    elif workflow.input == "pr":
        opened = inputs.open_pr(run, cfg, context)
        if opened.nothing_to_do:
            return run.finish(accepted=True)      # "already handled" is the common case
    else:
        with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                                   description="Capture the incoming ask, and which "
                                               "workflow was asked to carry it")) as ph:
            ph.log(input=request, workflow=workflow.name,
                   stages=" -> ".join(step.stage.name for step in workflow.steps))
        opened = inputs.Opened(prompt=request)

    ctx = StageContext(run, workflow, opened.prompt)
    ctx.previous = opened.previous
    ctx.baseline = run.pin("baseline", lambda: git_helper.rev(run.repo_root, "HEAD"))
    accepted, reason = True, ""
    try:
        for step in workflow.steps:
            ctx.begin(step)
            output = step.stage.run(ctx, step.opts)
            ctx.end(step, output)
    except StageStop as stop:
        accepted, reason = False, str(stop)

    # The tracker hears about the run either way: a run that could not finish
    # is exactly the one whose reporter most needs to know where it stopped.
    if workflow.input == "issue":
        inputs.report_issue(run, cfg, opened, accepted)
    elif workflow.input == "pr":
        build = ctx.latest.get(BuildOutput)
        inputs.report_pr(run, cfg, opened, accepted, build.summary if build else "")
    return run.finish(accepted=accepted, reason=reason)
