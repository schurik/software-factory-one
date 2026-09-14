"""The Claude Code harness.

Runs `claude -p --output-format stream-json --verbose` and tails its NDJSON
stdout line by line — structurally the same read loop the pi harness already
had, so events land in the trace while the agent is still working.

Two things differ from pi and shape everything below:

1. **`--session-id` is create-ONLY.** A second invocation with the same id
   fails with `Session ID <uuid> is already in use`; continuing takes
   `--resume <uuid>` instead. pi's one id covers both cases, Claude Code's
   does not, so the request carries `resume` and `agents.execute` keeps the
   `started` flag in the agent map. The id must also be a real UUID, which is
   why it is derived rather than minted from `new_id`.
2. **A default `claude -p` reads the operator's world** — CLAUDE.md, skills,
   plugins, hooks, MCP servers. A run that depends on whose machine it ran on
   is the failure the factory exists to remove, so `Options` below pins it off
   by default. Those are this harness's `harness_options:` block, and they are
   config rather than code: a repository that genuinely wants its own CLAUDE.md
   loaded says so there.

One harness behind the names `__init__.py` documents — `NAME`, `Options`,
`resolve_model`, `reachable`, `credentials`, `validate_agent`,
`new_session_id`, `ToolCallTracker`, `run`. Its templates (roster, prompts, env sample) live in
the skill under `templates/harnesses/claude_code/`.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..data_types import (AgentConfig, AgentRequest, AgentResult, Finding,
                          UsageBreakdown)
from ..limits import AgentTimeout, Deadline
from ..tool_calls import ToolCallLedger
from ..utils import operator_env

NAME = "claude_code"

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")


class Options(BaseModel):
    """`harness_options` for a Claude Code agent: determinism and permissions.

    A default `claude -p` discovers whatever the operator has lying around —
    CLAUDE.md, skills, plugins, hooks, MCP servers — which makes a run depend
    on whose machine it executed on. That is the exact failure the factory
    exists to remove, so the defaults below pin it off. They are configuration
    rather than code because some repositories genuinely do want their own
    CLAUDE.md loaded, and that is their decision to make.

    `extra="forbid"`: an unknown key here is a typo or a block written for
    another harness, and either one is worth failing validation over — a
    silently ignored `safe_mode` is a run that read the operator's world
    without saying so.
    """

    model_config = ConfigDict(extra="forbid")

    # --safe-mode: no CLAUDE.md, skills, plugins, hooks, MCP servers, custom
    # agents or commands. Auth, model selection, built-in tools and permissions
    # keep working, so a Claude subscription still authenticates.
    safe_mode: bool = True
    # --bare is stricter still (it also skips LSP and background prefetches),
    # but it forces ANTHROPIC_API_KEY / apiKeyHelper auth and never reads OAuth
    # or the keychain — so turning it on takes a subscription-authenticated
    # roster offline. Off by default for that reason; safe_mode covers the
    # determinism half without the auth cost.
    bare: bool = False
    setting_sources: list[str] = Field(default_factory=list)   # user | project | local
    strict_mcp_config: bool = True                             # only MCP servers we pass
    # A non-interactive run has to answer its own permission prompts. This is
    # only acceptable because two other things are true: permissions.py
    # fingerprints the tree before the call and rolls back every write outside
    # the agent's `writes:` allowlist afterwards, and the run happens in its own
    # worktree. The factory is not careless here; it verifies after the fact.
    # `bypassPermissions` is refused by the CLI when running as root — use
    # `acceptEdits` there, and know that it silently denies whatever it would
    # otherwise have prompted for.
    permission_mode: str = "bypassPermissions"
    add_dirs: list[str] = Field(default_factory=list)          # --add-dir, beyond cwd
    max_budget_usd: float = 0.0                                # 0 = no ceiling


# Deterministic session ids: the same adw_id + agent + model always resolves to
# the same UUID, so a re-run pinned to an adw_id lands on the session it left.
SESSION_NAMESPACE = uuid.UUID("6f1f3d7e-6f2a-5a5e-9c4b-3f4d5e6a7b8c")

# pi's tool vocabulary mapped onto Claude Code's. The names differ in case and
# in spelling, and a name that maps to nothing is a VALIDATION ERROR rather
# than a silent drop — an agent that quietly lost a capability is a correctness
# bug, and `--tools` filtering is exactly where capabilities disappear quietly.
#
# `ls` is the lossy one: Claude Code has no directory-listing tool. It maps to
# Glob, the read-only equivalent, and deliberately NOT to Bash — handing a
# read-only agent a shell to make one tool name resolve would turn a mapping
# table into a privilege escalation.
TOOL_MAP = {
    "read": "Read",
    "bash": "Bash",
    "edit": "Edit",
    "write": "Write",
    "grep": "Grep",
    "find": "Glob",
    "ls": "Glob",
}

# Claude Code's own tool names pass through untouched, so a roster written for
# this harness can name them directly instead of going through pi's words.
CLAUDE_TOOLS = {
    "Read", "Write", "Edit", "Bash", "BashOutput", "KillShell", "Glob", "Grep",
    "NotebookEdit", "WebFetch", "WebSearch", "Task", "TodoWrite", "SlashCommand",
    "Skill", "ExitPlanMode",
}

# pi's ladder has two rungs below Claude Code's. Both collapse onto `low` —
# there is no "no thinking" effort to map `off` onto — and the collapse is
# reported once per process so it is visible in the trace rather than inferred.
EFFORT_MAP = {"off": "low", "minimal": "low", "low": "low", "medium": "medium",
              "high": "high", "xhigh": "xhigh", "max": "max"}

# Events that carry nothing the trace wants. `commands_changed` is the big one:
# ~20 KB of skill and slash-command listings per invocation, which would dwarf
# the actual tool calls in `events.payload_json`. The rest are static per run
# (`autocompact_state`), UI chrome (`active_goal`), or a restatement of a turn
# that is already recorded (`post_turn_summary`, `task_summary`).
DROPPED_EVENTS = {"active_goal", "autocompact_state"}
DROPPED_SYSTEM_SUBTYPES = {"commands_changed", "post_turn_summary", "task_summary",
                           "thinking_tokens"}

_WARNED: set[str] = set()


# ── capability probes ────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _version() -> str:
    try:
        result = subprocess.run([CLAUDE_PATH, "--version"], capture_output=True,
                                text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def reachable() -> None:
    """Raise unless the claude CLI can be executed. Checked once per process."""
    if not _version():
        raise RuntimeError(
            f"the Claude Code CLI ({CLAUDE_PATH!r}) is not reachable — install it "
            f"(`npm i -g @anthropic-ai/claude-code`), put it on PATH, or set CLAUDE_PATH")


def credentials(agent: AgentConfig) -> list[Finding]:
    """Nothing to check: the Claude Code CLI carries its own authentication.

    Implemented rather than omitted so `doctor` says so out loud. "No key is
    needed here" is a useful answer — it is the reason a claude_code roster is
    the shortest path to a first green run, and an absent line would read as an
    unchecked one.
    """
    return [Finding(check=f"credentials: {agent.name}",
                    detail="claude_code — the CLI brings its own auth (`claude auth`)")]


def resolve_model(pattern: str) -> str:
    """Claude Code takes an alias or a full model id; there is no provider half.

    Nothing is resolved against a catalog — the CLI owns that list — so this
    only rejects what cannot possibly work. The one that matters is a
    `provider/model-id` pattern: a claude_code agent inheriting
    `defaults.model: google/gemini-3.6-flash` would otherwise fail deep inside
    a chain instead of at validation.
    """
    model = pattern.strip()
    if not model:
        raise ValueError("model is empty — name an alias (opus, sonnet, haiku) "
                         "or a full model id (claude-sonnet-5)")
    if "/" in model:
        raise ValueError(
            f"model {pattern!r} is a pi provider/model-id pattern; Claude Code takes "
            f"an alias (opus, sonnet, haiku) or a full model id (claude-sonnet-5). "
            f"A claude_code agent must set its own `model:` or the roster must not "
            f"default to a pi pattern.")
    return model


def map_tools(tools: Optional[list[str]]) -> Optional[list[str]]:
    """pi tool names -> Claude Code tool names. Raises on anything unmapped."""
    if tools is None:
        return None
    mapped, unknown = [], []
    for tool in tools:
        name = TOOL_MAP.get(tool) if tool not in CLAUDE_TOOLS else tool
        if name is None:
            unknown.append(tool)
        elif name not in mapped:
            mapped.append(name)
    if unknown:
        raise ValueError(
            f"tools {unknown} have no Claude Code equivalent — name Claude Code tools "
            f"directly ({', '.join(sorted(CLAUDE_TOOLS))}) or use pi's names "
            f"({', '.join(sorted(TOOL_MAP))}). An unmapped tool is refused rather than "
            f"dropped: an agent that silently lost a capability looks like a model that "
            f"stopped trying.")
    return mapped


def validate_agent(agent: AgentConfig) -> list[str]:
    """Harness-specific config problems for one agent. Empty list = fine."""
    problems = []
    try:
        options = Options(**agent.harness_options)
    except Exception as error:
        # Nothing below can be checked against options that would not parse, so
        # this one problem is the whole report for this agent.
        return [f"harness_options: {error}"]
    try:
        map_tools(agent.tools)
    except ValueError as error:
        problems.append(str(error))
    if agent.tools is not None and not agent.tools:
        problems.append("tools: [] leaves the agent with no tools at all — omit the "
                        "key for all tools, or name the ones it needs")
    if agent.thinking not in EFFORT_MAP:
        problems.append(f"thinking {agent.thinking!r} is not one of "
                        f"{' | '.join(EFFORT_MAP)}")
    if agent.harness_engineering and (options.safe_mode or options.bare):
        problems.append(
            "harness_engineering is set, but harness_options.safe_mode (or .bare) is on, "
            "which suppresses MCP servers, plugins and custom agents — verified: "
            "`--agents` passed under --safe-mode does not reach the session. Turn the "
            "switch off for this agent, or drop the harness entries.")
    for entry in agent.harness_engineering:
        if _harness_flag(str(entry)) is None:
            problems.append(
                f"harness_engineering {entry!r}: on Claude Code an entry is "
                f"`mcp:<file.json>`, `agents:<json-or-file>` or `plugin:<dir-or-zip>`. "
                f"Pi's TypeScript extensions have no equivalent here — the two "
                f"harnesses do not share this key.")
    if options.permission_mode not in ("acceptEdits", "auto", "bypassPermissions",
                                       "manual", "dontAsk", "plan"):
        problems.append(f"harness_options.permission_mode {options.permission_mode!r} is not "
                        f"a mode the CLI accepts")
    if options.permission_mode in ("manual", "plan"):
        problems.append(f"harness_options.permission_mode {options.permission_mode!r} needs a "
                        f"human at a terminal; a factory run has nobody to ask")
    for source in options.setting_sources:
        if source not in ("user", "project", "local"):
            problems.append(f"harness_options.setting_sources {source!r} is not one of "
                            f"user | project | local")
    return problems


def new_session_id(adw_id: str, agent: AgentConfig) -> str:
    """The UUID this agent's Claude Code session is known by.

    Derived, not random. Including the model keeps `agents.py`'s existing
    "model changed, new session" rule working without a special case, and makes
    a re-run pinned to the same `--adw-id` land on the session it left rather
    than opening a second one beside it.
    """
    return str(uuid.uuid5(SESSION_NAMESPACE, f"{adw_id}:{agent.name}:{agent.model}"))


# ── event handling ───────────────────────────────────────────────────────────

def _is_noise(event: dict) -> bool:
    """True for events that must never reach the trace, the JSONL or raw output."""
    etype = event.get("type", "")
    if etype in DROPPED_EVENTS:
        return True
    return etype == "system" and event.get("subtype") in DROPPED_SYSTEM_SUBTYPES


def _text_of(content) -> str:
    """Join the text of a Claude Code content field — a string, or blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(block.get("text", "") for block in content
                   if isinstance(block, dict) and block.get("type") == "text")


class ToolCallTracker:
    """Folds Claude Code's tool stream into ONE record per completed call.

    A call is announced as a `tool_use` block inside an `assistant` message and
    answered by a `tool_result` block inside the following `user` message. Only
    the answer carries the result, so that is where records are emitted — and
    one `user` message can answer several parallel calls at once, which is why
    `observe` returns a list.

    The record shape is tool_calls.py's, identical to the pi tracker's, which
    is what lets `agents._event_forwarder`, the tracer and the visualizer stay
    untouched by this harness existing.
    """

    def __init__(self) -> None:
        self._ledger = ToolCallLedger()

    def observe(self, event: dict) -> list[dict]:
        etype = event.get("type", "")
        if etype not in ("assistant", "user"):
            return []
        # `message` is an object on these two and a plain string elsewhere in
        # the stream (a warning, a rate-limit note), so neither the key nor its
        # type can be assumed.
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return []
        if etype == "assistant":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._ledger.announce(block.get("id"), block.get("name"),
                                          block.get("input"))
            return []
        return [self._ledger.close(block.get("tool_use_id"),
                                   ok=not block.get("is_error", False),
                                   result_text=_text_of(block.get("content")))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool_result"]


# ── usage ────────────────────────────────────────────────────────────────────

def _result_usage(event: dict) -> UsageBreakdown:
    """The terminal `result` event's usage as a UsageBreakdown.

    Claude Code reports `total_cost_usd` and nothing per component, so the four
    component costs stay at zero rather than being invented from a split the
    CLI never published. Any cross-harness cost view has to tolerate that:
    `total_cost` reconciles, `input_cost` and friends are pi-only.
    """
    usage = event.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    thinking = int((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
    return UsageBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=thinking,
        total_tokens=input_tokens + output_tokens + cache_read + cache_write,
        total_cost=float(event.get("total_cost_usd") or 0.0),
    )


def _turn_context_tokens(usage: dict) -> int:
    """How full the window is after an assistant turn.

    The same part-sum pi uses, in Claude Code's vocabulary: everything that is
    in the conversation now, cache reads included — cached prompt is still
    prompt — plus the turn the model just produced.
    """
    return int(sum(int(usage.get(part) or 0) for part in
                   ("input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens")))


def _context_window(event: dict, model: str) -> int:
    """The ceiling for the model the agent actually ran on.

    `modelUsage` also lists models Claude Code used for its own side work (a
    haiku summariser, for one), so the session's model is looked up by name
    first; the widest window is only a fallback when the key does not match.
    """
    usage = event.get("modelUsage") or {}
    for name, stats in usage.items():
        if name == model or stats.get("canonicalModel") == model:
            return int(stats.get("contextWindow") or 0)
    return max((int(stats.get("contextWindow") or 0) for stats in usage.values()),
               default=0)


# ── invocation ───────────────────────────────────────────────────────────────

def _harness_flag(entry: str) -> Optional[list[str]]:
    """One `harness_engineering` entry as CLI flags, or None if unrecognised.

    Pi extensions are TypeScript files loaded with `-e`; Claude Code's nearest
    equivalents are three unrelated things, so the entry says which it is
    rather than the code guessing from a file suffix.
    """
    kind, _, value = entry.partition(":")
    if not value:
        return None
    return {"mcp": ["--mcp-config", value],
            "agents": ["--agents", value],
            "plugin": ["--plugin-dir", value]}.get(kind)


def _argv(request: AgentRequest, model: str, options: Options) -> list[str]:
    """The full command line for one turn. Kept separate so it can be read."""
    cmd = [CLAUDE_PATH, "-p", "--output-format", "stream-json", "--verbose",
           "--model", model,
           "--effort", EFFORT_MAP[request.thinking],
           "--system-prompt", request.system_prompt,
           "--permission-mode", options.permission_mode]
    # Determinism. `--setting-sources` is passed even when empty: the empty
    # value is what turns user/project/local settings files OFF.
    cmd += ["--setting-sources", ",".join(options.setting_sources)]
    if options.bare:
        cmd.append("--bare")
    elif options.safe_mode:
        cmd.append("--safe-mode")
    if options.strict_mcp_config:
        cmd.append("--strict-mcp-config")
    # The run's tree is the cwd, but data_dir lives in the MAIN checkout, so
    # the session runtime is a SECOND root — and it is the one holding
    # context_handoff/, which is how agents hand work to each other. Without
    # this the file tools are confined to the worktree, the scout's findings
    # land somewhere the builder never looks, and the chain quietly degrades
    # into agents that cannot read each other.
    for directory in [d for d in [request.runtime_dir, *options.add_dirs] if d]:
        cmd += ["--add-dir", directory]
    if options.max_budget_usd:
        cmd += ["--max-budget-usd", str(options.max_budget_usd)]
    tools = map_tools(request.tools)
    if tools:
        cmd += ["--tools", ",".join(tools)]
    for entry in request.extensions:
        flags = _harness_flag(str(entry))
        if flags:
            cmd += flags
    session_id = request.native_session_id or request.session_id
    cmd += ["--resume", session_id] if request.resume else ["--session-id", session_id]
    cmd.append(request.prompt)
    return cmd


def _warn_once(key: str, message: str, on_event) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    if on_event:
        on_event({"type": "system", "subtype": "asf_warning", "message": message})


def _stream(cmd: list[str], request: AgentRequest, result: AgentResult,
            on_event, on_spawn, on_exit) -> tuple[int, str]:
    """Run one invocation, folding its NDJSON into `result`. Returns (rc, stderr)."""
    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    # stdin is DEVNULL for the same reason it is in agent_pi: the prompt travels
    # in argv, and an inherited stdin lets a non-interactive CLI sit forever
    # waiting for input that never arrives.
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=_child_env())
    if on_spawn:
        on_spawn(process.pid)
    model_id = ""
    # Every read below goes through the deadline, and none of them can block
    # forever: a CLI that stops emitting is the one failure this harness cannot
    # otherwise recover from — no events, no tokens, nothing in the trace, and
    # a run that sits there until somebody notices. limits.py has the why.
    deadline = Deadline(process, request.timeout_seconds)
    with raw_path.open("a") as raw:
        assert process.stdout is not None
        for line in deadline.lines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            # The filter runs BEFORE the raw file, not just before the tracer:
            # 20 KB of skill listings per invocation is noise wherever it lands.
            if _is_noise(event):
                continue
            raw.write(json.dumps(event) + "\n")
            raw.flush()                      # events land on disk as they happen
            etype = event.get("type")
            if etype == "system" and event.get("subtype") == "init":
                model_id = str(event.get("model") or "")
                result.session_id = str(event.get("session_id") or result.session_id)
            elif etype == "assistant":
                usage = event.get("message", {}).get("usage") or {}
                occupancy = _turn_context_tokens(usage)
                if occupancy:
                    result.context_tokens = occupancy
            elif etype == "result":
                # Per invocation, not cumulative — which is what makes these
                # sum across retries the same way pi's per-turn numbers do.
                turn = _result_usage(event)
                result.usage.merge(turn)
                result.tokens += turn.total_tokens
                result.cost += turn.total_cost
                result.context_window = (_context_window(event, model_id)
                                         or result.context_window)
                result.session_id = str(event.get("session_id") or result.session_id)
                if not event.get("is_error"):
                    result.text = str(event.get("result") or result.text)
            if on_event:
                on_event(event)

    stderr = deadline.drain(process.stderr)
    returncode = deadline.wait()
    if on_exit:
        on_exit(process.pid)
    if deadline.fired:
        # Ahead of the returncode, and it has to be: a terminated child exits
        # non-zero, so without this the timeout would surface as an ordinary
        # "claude exited -15" with no hint that the factory itself ended it.
        # Whatever the agent did emit is already in raw_output.jsonl and in the
        # trace; what is lost is this turn's usage, which Claude Code only
        # reports in the final `result` event that never came.
        raise AgentTimeout(deadline.reason(NAME, request.raw_output_path), result)
    return returncode, stderr


def _child_env() -> dict[str, str]:
    """The operator's environment, minus the ADW's own uv venv.

    Same reasoning as `utils.operator_env`, plus one Claude-Code-specific
    removal: this process may itself be running inside a Claude Code session,
    and inheriting that session's entrypoint marker makes the child believe it
    was launched by the parent's harness.
    """
    env = operator_env()
    for key in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE"):
        env.pop(key, None)
    return env


def run(request: AgentRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> AgentResult:
    """Run one non-interactive Claude Code turn.

    `on_spawn(pid)` and `on_exit(pid)` bracket the child process so the caller
    can record it as killable — a hung coding agent is otherwise a pid you have
    to hunt for in `ps` while the run sits there.
    """
    model = resolve_model(request.model)
    if EFFORT_MAP[request.thinking] != request.thinking:
        _warn_once(f"effort:{request.thinking}",
                   f"thinking {request.thinking!r} has no Claude Code equivalent; "
                   f"running at effort {EFFORT_MAP[request.thinking]!r}", on_event)
    options = Options(**request.options)
    result = AgentResult(session_id=request.native_session_id or request.session_id)

    returncode, stderr = _stream(_argv(request, model, options), request, result,
                                 on_event, on_spawn, on_exit)
    # The one recoverable failure: the session exists but the agent map did not
    # say so — a run killed between its first send and the map being written,
    # or a session dir carried over. Creating is what failed, so resume instead.
    if returncode != 0 and not request.resume and "already in use" in stderr:
        _warn_once(f"resume:{result.session_id}",
                   f"session {result.session_id} already existed; resuming it", on_event)
        resumed = request.model_copy(update={"resume": True})
        returncode, stderr = _stream(_argv(resumed, model, options), request, result,
                                     on_event, on_spawn, on_exit)

    result.returncode = returncode
    if returncode != 0 and not result.text:
        raise RuntimeError(f"claude exited {returncode}: {stderr.strip()[-800:]}")
    return result
