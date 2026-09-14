"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import yaml

from . import (artifacts, git_helper, harnesses, limits, permissions, preflight,
               prompts)
from .data_types import (AgentCall, AgentConfig, AgentRequest, AgentResult,
                         AgentSession, EnvelopeBase, EventRecord, GateCheck,
                         GateReport, Phase, RecordedPhase, FactoryConfig,
                         UsageBreakdown)
from .utils import anchor

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON


class GateFailure(RuntimeError):
    pass


def harness_for(agent: AgentConfig):
    """The harness module this agent runs on. The whole of the seam is here:
    `engine/harnesses/__init__.py` documents the names a module must
    expose, and nothing else in the factory knows which one is running."""
    try:
        return harnesses.HARNESSES[agent.harness]
    except KeyError:
        raise SystemExit(f"agent {agent.name!r}: harness {agent.harness!r} is not "
                         f"one of {' | '.join(harnesses.NAMES)}") from None


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "asf/factory.yaml") -> FactoryConfig:
    """Read a single-file config (defaults + agents list), merging defaults in.

    The stamped layout keeps agents in `asf/agents/<name>/` instead, and
    `engine.factory.load` assembles the same raw shape from those files before
    handing it to `merge_defaults` — one merge, two ways to write a roster.
    """
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return merge_defaults(raw)


def merge_defaults(raw: dict) -> FactoryConfig:
    """Merge each agent over `defaults` key by key and build the config."""
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        for key in ("harness", "model", "thinking", "color", "tools", "writes",
                    "timeout_seconds"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))
        # `defaults.harness_options` is keyed by harness name; an agent's own
        # block is flat, for the harness it actually runs on. So the inherited
        # half is looked up by that name, and the agent's keys win INDIVIDUALLY
        # — overriding `permission_mode` must not silently drop `safe_mode`.
        inherited = (defaults.get("harness_options") or {}).get(agent.get("harness"), {})
        agent["harness_options"] = {**(inherited or {}),
                                    **(agent.get("harness_options") or {})}
    return FactoryConfig(**raw)


def resolve(cfg: FactoryConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def validate(cfg: FactoryConfig, required: list[str]) -> None:
    """Fail fast: every required name must resolve to a usable agent.

    Prompt files are looked for in the MAIN checkout, which is where the roster
    and its prompts live — not in the run's worktree, and not in whatever
    directory the ADW was launched from.

    Everything harness-specific is asked of the harness: pi resolves a model
    against its catalog and Claude Code accepts an alias, pi's tool names are
    lowercase and Claude Code's are not, and only one of the two can load a
    TypeScript extension. The shape here — collect every problem, raise one
    SystemExit — is unchanged, and every ADW depends on it.

    A model is checked for being WRITTEN correctly AND for having a credential
    behind it — `preflight.credentials` asks the harness whether the variable
    its provider needs is set, never what is in it. That check is only fatal
    when the harness KNOWS the variable's name; a guess warns instead, because
    refusing to start on a guess is worse than the failure it was guessing at.
    Nothing here confirms the provider actually answers.
    """
    root = git_helper.main_root()
    problems, warnings = [], []
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        driver = harnesses.HARNESSES.get(agent.harness)
        if driver is None:
            problems.append(f"agent {name!r}: harness {agent.harness!r} is not "
                            f"one of {' | '.join(harnesses.NAMES)}")
            continue
        refs = [("system", agent.prompt_engineering.system)]
        refs += [("system_append", ref) for ref in agent.prompt_engineering.system_append]
        if agent.prompt_engineering.user:
            refs.append(("user", agent.prompt_engineering.user))
        for label, ref in refs:
            if not anchor(root, ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        # Model and tool vocabularies belong to the harness: pi resolves against
        # its catalog, Claude Code takes an alias. Applying either rule to the
        # other harness is how a valid roster gets rejected — or worse, a
        # nonsense one accepted.
        try:
            driver.resolve_model(agent.model)
        except ValueError as e:
            problems.append(f"agent {name!r}: {e}")
        problems += [f"agent {name!r}: {problem}"
                     for problem in driver.validate_agent(agent)]
        # Harness-agnostic, so it is checked here rather than in a driver. A
        # negative wall clock is a typo that would otherwise read as `0` — no
        # limit — which is the opposite of what whoever typed it meant.
        if agent.timeout_seconds < 0:
            problems.append(f"agent {name!r}: timeout_seconds must be >= 0 "
                            f"(0 disables it), got {agent.timeout_seconds}")
        try:
            driver.reachable()      # cached per harness; one probe per process
        except RuntimeError as e:
            problems.append(f"agent {name!r}: {e}")
        # The credential behind the model. Asked here rather than in
        # session.ensure() because this is the one place that knows WHICH
        # agents this chain will actually spawn — a key missing for an agent
        # nobody calls is not this run's problem.
        for finding in preflight.credentials(agent):
            if finding.level == "fatal":
                problems.append(f"{finding.detail}\n  fix: {finding.fix}")
            elif finding.level == "warn":
                warnings.append(finding)
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))
    for finding in warnings:
        print(f"  ~ {finding.detail}\n    {finding.fix}", file=sys.stderr)


# ── execution ────────────────────────────────────────────────────────────────

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> harness run -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)

    replayed = _replay(run, phase, call, agent.name)
    if replayed is not None:
        return replayed

    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        # Absolute, and it has to be. One worktree per RUN, not per agent: every
        # agent in a session is spawned in the same `run.repo_root`, and hands
        # its work to the next one through this directory — which lives under
        # data_dir in the MAIN checkout, outside that worktree.
        #
        # So a relative path would not lose the agents each other; they would
        # all resolve it identically, to a directory inside the worktree. It
        # would lose them the CODE. `changes.capture` and the quality blocks
        # write to `run.context_handoff_dir` and the trace records it, so the
        # two halves of one handoff would be in different trees. Worse, the
        # agents' half would then sit inside the tree that `commit_all` stages
        # with `git add -A` and that permissions.py fingerprints — a scout with
        # `writes: []` would breach its own boundary by filing its report — and
        # it would be deleted with the worktree when the run is released.
        "context_handoff_dir": str(run.context_handoff_dir),
    }
    variables.update(call.variables)
    # The roster's prompts live beside the config, in the main checkout. The
    # identity is the roster's file plus whatever the workflow appended; the
    # task is the file the stage resolved for THIS call.
    system_text = "\n\n".join(
        prompts.render(anchor(run.main_root, ref), variables).rstrip()
        for ref in [agent.prompt_engineering.system, *agent.prompt_engineering.system_append]
    ) + "\n"
    task_ref = call.task or agent.prompt_engineering.user
    if not task_ref:
        raise RuntimeError(f"agent {agent.name!r}: this call names no task and the agent "
                           f"has no fallback user prompt — a stage must resolve a task "
                           f"file before it calls an agent")
    user_text = prompts.render(anchor(run.main_root, task_ref), variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)

    driver = harness_for(agent)
    session = _agent_session(run, agent, driver)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": session.session_id,
                                          "harness": agent.harness,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "timeout_seconds": agent.timeout_seconds,
                                          "harness_engineering": agent.harness_engineering}))
    run.console.agent_started(agent.name, agent.model, session.session_id)

    # Parse retries and gate corrections re-enter the SAME agent session, so the
    # last send is the one whose context occupancy is current — while spend is
    # the opposite: every send costs, so usage accumulates across all of them.
    latest: AgentResult | None = None
    spent = UsageBreakdown()
    forward = _event_forwarder(run, phase, agent.name, driver)

    def send(prompt_text: str) -> AgentResult:
        nonlocal latest
        # Asked BEFORE the turn, because spend is only known after one is paid
        # for: a session that has hit its ceiling keeps the envelope it already
        # bought and dies here instead, rather than mid-turn with nothing to
        # show for the money. limits.py has the full argument.
        _refuse_if_over_budget(run, phase, agent)
        request = AgentRequest(
            prompt=prompt_text,
            system_prompt=system_text,
            model=agent.model,
            thinking=agent.thinking,
            session_id=session.session_id,
            # absolute: these are read by the coding-agent subprocess, which
            # runs in repo_root
            session_dir=str((agent_dir / f"{agent.harness}_sessions").resolve()),
            raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
            runtime_dir=str(run.session_dir.resolve()),
            tools=agent.tools,
            extensions=agent.harness_engineering,
            cwd=str(run.repo_root),
            native_session_id=session.native_session_id,
            resume=session.started,
            options=agent.harness_options,
            timeout_seconds=agent.timeout_seconds,
        )
        try:
            result = driver.run(
                request,
                on_event=forward,
                on_spawn=lambda pid: _spawned(run, agent, pid),
                on_exit=lambda pid: _exited(run, pid))
        except limits.AgentTimeout as expiry:
            # The turn produced no envelope, but it was not free. Bank what it
            # did spend before failing the phase, or the next run in this
            # session inherits a ceiling that never saw the money go.
            if expiry.result:
                run.add_usage(expiry.result.tokens, expiry.result.cost)
                spent.merge(expiry.result.usage)
            _record_limit(run, phase, agent, "agent_timeout", str(expiry))
            raise
        run.add_usage(result.tokens, result.cost)
        spent.merge(result.usage)
        latest = result
        # The session now EXISTS, and the next send in this phase must continue
        # it rather than create it again. Persisted immediately, not at the end
        # of the phase: a Claude Code session survives the process, so a run
        # that dies mid-phase would otherwise leave a map claiming a session it
        # can no longer create and cannot resume.
        session.native_session_id = result.session_id or session.native_session_id
        session.started = True
        _remember(run, agent, session)
        return result

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline.
    tree_before = permissions.snapshot(run)

    result = send(user_text)
    envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # claim gates — violations flow back into the SAME session as corrections
    for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
        violations = _check_gates(run, phase, call, envelope, gate_attempt)
        if not violations:
            break
        if gate_attempt > phase.params.retries:
            raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                              + "\n- ".join(violations))
        phase.attempt = gate_attempt
        run.console.retry(agent.name, gate_attempt, phase.params.retries,
                          f"{len(violations)} gate violation(s)")
        correction = ("Your previous response failed validation:\n- "
                      + "\n- ".join(violations)
                      + "\n\nFix these problems — use whatever tools you need (move a file, "
                        "rewrite content, re-run a command) to make them true on disk — then "
                        "re-emit ONLY your Report JSON as your final message.")
        result = send(correction)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # Permission is checked after every send is done, and before the envelope is
    # accepted: an agent does not get to report success on a phase in which it
    # wrote somewhere it was not allowed to.
    try:
        touched = permissions.enforce(run, phase, agent, tree_before)
    except permissions.PermissionBreach as breach:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="error", name="permission_breach",
                                     payload={"agent": agent.name, "error": str(breach),
                                              "writes": agent.writes,
                                              "protected_files": run.cfg.defaults.protected_files}))
        raise
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))

    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, session.session_id,
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    _remember(run, agent, session)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent.name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_end", name=agent.name,
                                 # Phase totals, not the last send's: a retried
                                 # phase paid for every attempt.
                                 tokens=spent.total_tokens,
                                 payload={"cost": spent.total_cost,
                                          "usage": spent.model_dump(),
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


# ── internals ────────────────────────────────────────────────────────────────

def _refuse_if_over_budget(run, phase: Phase, agent: AgentConfig) -> None:
    """Stop the session before it pays for another turn. No-op with no ceiling.

    Raises rather than degrading — there is no partial version of an agent
    turn, and a chain that quietly skipped one would hand the next agent an
    envelope nobody produced. The phase fails, the run aborts, and the worktree
    is kept: the work bought so far is on its branch, and `just integrate`
    still lands it.
    """
    reason = run.overrun()
    if not reason:
        return
    _record_limit(run, phase, agent, "budget_exceeded", reason)
    raise limits.BudgetExceeded(f"{agent.name} not sent: {reason}")


def _record_limit(run, phase: Phase, agent: AgentConfig, kind: str, reason: str) -> None:
    """One error event per limit that fired, named for the limit.

    The phase records its own failure either way (runner.py), but only as the
    exception's text. A dedicated event is what makes "which agents time out"
    and "how often does a ceiling stop a run" answerable from the trace, the
    way `permission_breach` already is for the write boundary.
    """
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="error", name=kind,
                                 payload={"agent": agent.name, "reason": reason,
                                          "timeout_seconds": agent.timeout_seconds,
                                          "max_cost_usd": run.cfg.budget.max_cost_usd,
                                          "max_tokens": run.cfg.budget.max_tokens}))
    run.console.note(f"{kind}: {reason}")


def _spawned(run, agent: AgentConfig, pid: int) -> None:
    """A coding agent child, recorded in both places `just kill` might look.

    The file is the one that matters — it is what `kill_run.py` reads, and it is
    there on a machine with no db — while the row keeps the trace UI's process
    view complete.
    """
    command = f"{agent.harness} {agent.name} {agent.model}"
    run.tracer.process_start(run.adw_id, "agent", agent.name, pid, command)
    artifacts.record_process(run.session_dir, "agent", agent.name, pid, command)


def _exited(run, pid: int) -> None:
    run.tracer.process_end(run.adw_id, pid)
    artifacts.end_process(run.session_dir, pid)


def _check_gates(run, phase: Phase, call: AgentCall, envelope: EnvelopeBase,
                 attempt: int) -> list[str]:
    """Run this call's gates once against `envelope`. Returns every violation.

    One implementation, two callers: the correction loop above, and the replay
    check below. A resumed run's recorded envelope has to clear the SAME gates
    the live one would — measured against the tree as it is now, not as it was
    — or the resume would be the one path in the factory where a claim is taken
    on trust.
    """
    violations: list[str] = []
    for gate in call.gates:
        report = _as_report(gate(envelope, run))
        found = report.violations
        run.tracer.gate_row(phase, gate.__name__, report, attempt)
        run.tracer.event(EventRecord(
            adw_id=run.adw_id, phase_id=phase.phase_id,
            type="gate_fail" if found else "gate_pass", name=gate.__name__,
            payload={"attempt": attempt, "violations": found,
                     "checks": [c.model_dump() for c in report.checks]}))
        run.console.gate_result(gate.__name__, report)
        violations.extend(found)
    return violations


def _replay(run, phase: Phase, call: AgentCall, agent_name: str) -> Optional[EnvelopeBase]:
    """The recorded answer to this phase, if a resumed run may still use it.

    None means "call the agent" — including the case where a record existed and
    its gates no longer hold, which is the whole safety story of a resume: the
    worktree may have been pruned and re-created from the branch, taking the
    uncommitted half of a build with it, and a replayed envelope claiming files
    that are no longer there is caught by the same gate that would have caught
    the agent inventing them.

    A replayed phase is written to the trace like any other — envelope row,
    envelope.json, handoff event — because everything downstream reads the
    record, not this function. What it does NOT write is usage: no agent ran, so
    the phase costs nothing and says so.
    """
    envelope = run.replay.envelope_for(phase, call.output_type)
    if envelope is None:
        return None
    record = run.replay.records[phase.params.name]
    if _check_gates(run, phase, call, envelope, attempt=0):
        run.console.note(f"replay rejected by its gates — running {agent_name} for real")
        return None
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="replay", name=agent_name,
                                 payload={"source_seq": record.seq,
                                          "source_phase": record.phase,
                                          "output_type": record.output_type,
                                          "agent": agent_name}))
    run.console.replayed(phase.params.name, record.seq)
    _persist_envelope(run, phase, agent_name, call, envelope, attempt=0, valid=True)
    run.console.envelope_summary(envelope)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent_name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    return envelope


def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _agent_session(run, agent: AgentConfig, driver) -> AgentSession:
    """This agent's context window in this run: rejoined, or freshly minted.

    The pre-existing rule is that a session is reused only while the MODEL is
    unchanged — a context window built by one model is not one another model
    should inherit. The harness is part of that identity for the same
    reason, and more bluntly: a pi session id is not a UUID and Claude Code
    would refuse it outright.
    """
    entry = run.agent_map.get(agent.name) or {}
    if entry.get("model") == agent.model and entry.get("harness") == agent.harness:
        return AgentSession(session_id=entry["session_id"],
                            native_session_id=entry.get("native_session_id", ""),
                            started=bool(entry.get("started")))
    session_id = driver.new_session_id(run.adw_id, agent)
    return AgentSession(session_id=session_id, native_session_id=session_id)


def _remember(run, agent: AgentConfig, session: AgentSession) -> None:
    """Write this agent's session state into the run's agent map."""
    run.save_agent_map(agent.name, {"session_id": session.session_id,
                                    "model": agent.model,
                                    "harness": agent.harness,
                                    "native_session_id": session.native_session_id,
                                    "started": session.started})


def _event_forwarder(run, phase: Phase, agent_name: str, driver):
    """One tool_call event per real tool call, with its exact args and result.

    The tracker comes from the harness; the record shape does not (it is
    tool_calls.py's, identical for both), which is what keeps the tracer, the
    trace schema and the visualizer out of this phase entirely.
    """
    tracker = driver.ToolCallTracker()

    def forward(event: dict) -> None:
        for record in tracker.observe(event):
            # The call's span rides the columns; duration_ms stays in the
            # payload as the coding agent's own authoritative number.
            run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                         type="tool_call", name=record.pop("label"),
                                         started_at=record.pop("started_at", None),
                                         ended_at=record.pop("ended_at", None),
                                         payload={**record, "agent": agent_name}))
    return forward


def _extract_json(text: str) -> dict:
    candidate = text
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            if block.startswith("{"):
                candidate = block
                break
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    return json.loads(candidate[start:end + 1])


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, JSON_FIX_ATTEMPTS,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if not envelope:
        return
    record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
              "output_type": call.output_type.__name__, "attempt": attempt,
              **envelope.model_dump()}
    (run.session_dir / agent_name / "envelope.json").write_text(json.dumps(record, indent=2))
    # And once more keyed by the PHASE. The file above is last-wins per agent —
    # right for "what did the builder last say", useless for a resume, where a
    # builder that built, fixed and revised has to answer three phases. This one
    # is what `engine/replay.py` reads back.
    artifacts.write_envelope(run.session_dir, RecordedPhase(
        phase_id=phase.phase_id, seq=phase.seq, phase=phase.params.name,
        agent=agent_name, output_type=call.output_type.__name__,
        payload_json=envelope.model_dump_json()))


def load_envelope(run, agent_name: str, output_type: type[EnvelopeBase]) -> EnvelopeBase:
    """Reload an agent's persisted envelope as a typed object — the read side
    of `_persist_envelope`, for an ADW that resumes work an EARLIER run already
    produced under the same `--adw-id`, rather than calling that agent again.

    Ignores the four bookkeeping keys `_persist_envelope` writes alongside the
    envelope's own fields (`agent_name`, `purpose`, `output_type`, `attempt`) —
    pydantic drops unknown fields on `model_validate` by default.
    """
    path = run.session_dir / agent_name / "envelope.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no envelope for {agent_name!r} in session {run.adw_id} ({path}) — "
            f"it has not produced one in this session yet")
    return output_type.model_validate(json.loads(path.read_text()))
