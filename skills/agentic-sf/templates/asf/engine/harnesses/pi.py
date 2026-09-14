"""The pi harness.

Runs `pi -p --mode json` and tails its JSONL stdout line by line, forwarding
each event to a callback WHILE the agent works (the streaming crack, solved
by construction). `--session-id` creates-or-continues, so running and
continuing an agent are the same call: same session id = same context window.

One harness behind the names `__init__.py` documents — `NAME`, `Options`,
`resolve_model`, `reachable`, `credentials`, `validate_agent`,
`new_session_id`, `ToolCallTracker`, `run` — which is all `agents.py` and
`preflight.py` dispatch on. Its
templates (roster, prompts, extensions, env sample) live in the skill under
`templates/harnesses/pi/`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict

from ..data_types import (AgentConfig, AgentRequest, AgentResult, Finding,
                          UsageBreakdown)
from ..limits import AgentTimeout, Deadline
from ..tool_calls import ToolCallLedger
from ..utils import new_id, operator_env

NAME = "pi"


class Options(BaseModel):
    """`harness_options` for a pi agent — empty, and deliberately strict.

    Pi is configured through argv and the environment (`PI_PATH`,
    `PI_MODELS_PATH`, the provider key its `model` names), so there is nothing
    per-agent to carry here yet. `extra="forbid"` is the point: a claude_code
    block pasted onto a pi agent fails validation instead of being silently
    ignored, which is exactly the mistake a mixed roster invites.
    """

    model_config = ConfigDict(extra="forbid")


PI_PATH = os.environ.get("PI_PATH", "pi")
MODELS_JSON = os.environ.get("PI_MODELS_PATH",
                             str(Path.home() / ".pi" / "agent" / "models.json"))

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def _count(value: str) -> int:
    """Parse pi's compact model-list counts (`272K`, `1.0M`)."""
    suffixes = {"K": 1_000, "M": 1_000_000}
    suffix = value[-1:].upper()
    if suffix in suffixes:
        return int(float(value[:-1]) * suffixes[suffix])
    return int(value)


@lru_cache(maxsize=1)
def _pi_catalog() -> list[tuple[str, str, int]]:
    """Read pi's merged catalog, including built-in providers and custom models."""
    try:
        result = subprocess.run(
            [PI_PATH, "--list-models"], capture_output=True, text=True,
            timeout=30, env=operator_env(), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) < 3:
            continue
        try:
            rows.append((columns[0], columns[1], _count(columns[2])))
        except ValueError:
            continue
    return rows


def resolve_model(pattern: str) -> tuple[str, str]:
    """Resolve a model pattern to an explicit ``(provider, model_id)`` pair.

    Pi's catalog merges built-in models with ``~/.pi/agent/models.json``. Using
    that same merged view lets the factory target direct providers such as
    ``openai/gpt-5.6-terra`` without re-registering built-in models locally.
    """
    catalog = [(provider, model_id) for provider, model_id, _ in _pi_catalog()]
    if "/" in pattern:
        provider, model_id = pattern.split("/", 1)
        if (provider, model_id) in catalog:
            return provider, model_id
    matches = [(provider, model_id) for provider, model_id in catalog
               if pattern == model_id or pattern in model_id]
    exact = [match for match in matches
             if match[1] == pattern or match[1].endswith("/" + pattern)]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"model pattern {pattern!r} not found in pi --list-models — "
                         "authenticate/register it or fix the config")
    raise ValueError(f"model pattern {pattern!r} is ambiguous: {matches}")


# Which environment variable carries a provider's key is pi's business, not
# ours: it comes from ~/.pi/agent/models.json, where a provider block names it.
# So the name is read from there when it is declared, and only guessed when it
# is not — and a guess never fails a run, it warns. `PROVIDER_API_KEY` is the
# convention every provider in the starter roster happens to follow, which is
# exactly why it is not trustworthy enough to be fatal.
KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]*(?:KEY|TOKEN)[A-Z0-9_]*$")


def _declared_key_env(provider: str) -> str:
    """The env var models.json says pi reads for `provider`, or "".

    Deliberately shape-matched rather than field-matched: pi is free to call the
    field `apiKey`, `apiKeyEnv` or `envVar`, and all three mean the same thing.
    A value that is an actual secret rather than a variable NAME cannot match —
    it is not an all-caps identifier ending in KEY or TOKEN — so nothing here
    can read, log or leak a key. Only names are ever touched.
    """
    try:
        registry = json.loads(Path(MODELS_JSON).read_text())
    except (OSError, ValueError):
        return ""
    block = registry.get("providers", {}).get(provider, {})
    if not isinstance(block, dict):
        return ""
    for field, value in block.items():
        if not isinstance(value, str):
            continue
        if any(word in field.lower() for word in ("key", "token", "env")) \
                and KEY_NAME.match(value):
            return value
    return ""


def _guessed_key_env(provider: str) -> str:
    """The conventional name, for a provider models.json does not describe."""
    slug = "".join(char if char.isalnum() else "_" for char in provider).upper()
    return f"{slug}_API_KEY"


def credentials(agent: AgentConfig) -> list[Finding]:
    """Whether the key this agent's model needs is actually set.

    The gap this closes: `resolve_model` proves a model is written correctly and
    exists in pi's catalog, and nothing proved the credential behind it exists —
    so a missing key surfaced when that agent ran, which on a five-phase chain is
    after the planner has been paid for.

    Never reads a key's value. The variable's name is the whole answer.
    """
    try:
        provider, _ = resolve_model(agent.model)
    except ValueError:
        return []                    # agents.validate() already reports this one
    declared = _declared_key_env(provider)
    name = declared or _guessed_key_env(provider)
    if os.environ.get(name, "").strip():
        return [Finding(check=f"credentials: {agent.name}",
                        detail=f"{provider} → ${name} is set")]
    if declared:
        return [Finding(
            check=f"credentials: {agent.name}", level="fatal",
            detail=f"agent {agent.name!r} runs {agent.model} on provider {provider!r}, "
                   f"whose key ${name} is not set",
            fix=f"add {name}=… to .env (models.json is what names it), or point this "
                f"agent at a provider you have a key for")]
    return [Finding(
        check=f"credentials: {agent.name}", level="warn",
        detail=f"agent {agent.name!r} runs on provider {provider!r} and ${name} is "
               f"not set — {MODELS_JSON} does not name the variable pi reads for it, "
               f"so this is a guess",
        fix=f"set {name} in .env if that is the right name; if pi reads a different "
            f"one for {provider!r}, this warning is safe to ignore")]


def reachable() -> None:
    """Raise unless the pi CLI can be executed. Checked once per process."""
    if not _pi_catalog():
        raise RuntimeError(
            f"the pi CLI ({PI_PATH!r}) is not reachable, or `pi --list-models` "
            f"returned nothing — install it, put it on PATH, or set PI_PATH")


def validate_agent(agent: AgentConfig) -> list[str]:
    """Harness-specific config problems for one agent. Empty list = fine."""
    problems = []
    try:
        Options(**agent.harness_options)
    except Exception as error:
        problems.append(f"harness_options: {error}")
    if agent.thinking not in THINKING_LEVELS:
        problems.append(f"thinking {agent.thinking!r} is not one of "
                        f"{' | '.join(THINKING_LEVELS)}")
    for extension in agent.harness_engineering:
        if not str(extension).endswith(".ts"):
            problems.append(f"harness_engineering {extension!r}: pi extensions are "
                            f"TypeScript files passed as `pi -e <file.ts>`")
    return problems


def new_session_id(adw_id: str, agent: AgentConfig) -> str:
    """A fresh pi session id. Random, because pi's ids are create-or-continue:
    a deterministic one would silently rejoin a context window from an earlier
    run whenever the agent map went missing."""
    return f"asf-{adw_id}-{agent.name}-{new_id(4)}"


def _turn_usage(usage: dict, total_tokens: int) -> UsageBreakdown:
    """Pi's `message_end` usage object as a UsageBreakdown.

    `total_tokens` is passed in rather than re-derived: the caller already
    computes it pi's way (totalTokens, else the sum of the parts). Pi is the
    harness that reports cost per component, so all five cost fields are real.
    """
    cost = usage.get("cost") or {}
    return UsageBreakdown(
        input_tokens=usage.get("input") or 0,
        output_tokens=usage.get("output") or 0,
        cache_read_tokens=usage.get("cacheRead") or 0,
        cache_write_tokens=usage.get("cacheWrite") or 0,
        reasoning_tokens=usage.get("reasoning") or 0,
        total_tokens=total_tokens,
        input_cost=cost.get("input") or 0.0,
        output_cost=cost.get("output") or 0.0,
        cache_read_cost=cost.get("cacheRead") or 0.0,
        cache_write_cost=cost.get("cacheWrite") or 0.0,
        total_cost=cost.get("total") or 0.0,
    )


def _context_tokens(usage: dict) -> int:
    """Tokens occupying the window after a turn.

    Mirrors pi's own `calculateContextTokens` (coding-agent
    `core/compaction/compaction.ts`), which is what pi compacts against and
    shows in its footer: prefer the provider's `totalTokens`, else sum the
    parts. Cache reads count — cached prompt is still prompt.
    """
    total = usage.get("totalTokens") or 0
    if total:
        return int(total)
    return int(sum(usage.get(part) or 0
                   for part in ("input", "output", "cacheRead", "cacheWrite")))


def context_window(provider: str, model_id: str) -> int:
    """The model's context ceiling from pi's merged model catalog.

    ``models.json`` is optional — a provider registered entirely by a pi
    extension (no local overrides) has no such file, so a missing file falls
    straight through to ``pi --list-models`` below rather than raising.
    """
    try:
        registry = json.loads(Path(MODELS_JSON).read_text())
    except FileNotFoundError:
        registry = {}
    for model in registry.get("providers", {}).get(provider, {}).get("models", []):
        if model.get("id") == model_id:
            return int(model.get("contextWindow") or 0)
    for listed_provider, listed_model, window in _pi_catalog():
        if listed_provider == provider and listed_model == model_id:
            return window
    return 0


def _text_of(container: dict) -> str:
    """Join the text blocks of anything pi shapes as {content: [...]} — a
    message or a tool result."""
    return "".join(part.get("text", "") for part in container.get("content", []) or []
                   if isinstance(part, dict) and part.get("type") == "text")


class ToolCallTracker:
    """Folds pi's tool stream into ONE normalized record per completed call.

    pi announces a call as a `toolCall` content block, then emits
    tool_execution_start / _update / _end for it. Only the end carries the
    result, so that is where a record is emitted — one trace event per real
    tool call, the moment it returns, instead of three shapeless ones.

    `observe` returns a LIST because another harness can close several calls
    in one event; the record shape itself lives in tool_calls.py, which is what
    keeps every harness indistinguishable downstream.
    """

    def __init__(self) -> None:
        self._ledger = ToolCallLedger()

    def observe(self, event: dict) -> list[dict]:
        """Records for whatever tool calls this event finished — usually none."""
        etype = event.get("type", "")
        if etype == "message_end":
            for block in event.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    self._ledger.announce(block.get("id"), block.get("name"),
                                          block.get("arguments"))
            return []
        if etype == "tool_execution_start":
            self._ledger.announce(event.get("toolCallId"), event.get("toolName"),
                                  event.get("args"))
            return []
        if etype != "tool_execution_end":
            return []
        return [self._ledger.close(event.get("toolCallId"),
                                   tool=str(event.get("toolName") or ""),
                                   args=event.get("args"),
                                   ok=not event.get("isError", False),
                                   result_text=_text_of(event.get("result") or {}))]


def run(request: AgentRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> AgentResult:
    """Run one non-interactive pi turn.

    `on_spawn(pid)` and `on_exit(pid)` bracket the child process so the caller
    can record it as killable — a hung coding agent is otherwise a pid you have
    to hunt for in `ps` while the run sits there.
    """
    provider, model_id = resolve_model(request.model)
    cmd = [
        PI_PATH, "-p", "--mode", "json",
        "--provider", provider, "--model", model_id,
        "--thinking", request.thinking,
        "--session-id", request.session_id,
        "--session-dir", request.session_dir,
        "--system-prompt", request.system_prompt,
    ]
    if request.tools:
        cmd += ["--tools", ",".join(request.tools)]
    for extension in request.extensions:
        cmd += ["-e", extension]
    cmd.append(request.prompt)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = AgentResult(session_id=request.session_id,
                         context_window=context_window(provider, model_id))
    # stdin is DEVNULL, deliberately. The prompt travels in argv, so the child
    # never needs stdin — but inheriting the parent's means pi sees a non-TTY
    # and can sit forever waiting for piped input that will never arrive or
    # EOF. That failure is silent and total: no request goes out, no bytes come
    # back, and the ADW blocks on a read loop with nothing to read. Observed as
    # a run that sat idle at 0% CPU with an empty raw_output.jsonl.
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
    # A hung pi emits nothing, and every read below would block on nothing
    # forever — the failure the DEVNULL comment above describes, arriving by a
    # route stdin cannot fix. The deadline is what ends it: it owns the clock
    # on this side of the pipe, so no read waits on the child's goodwill.
    deadline = Deadline(process, request.timeout_seconds)
    with raw_path.open("a") as raw:
        assert process.stdout is not None
        for line in deadline.lines():
            raw.write(line)
            raw.flush()                      # events land on disk as they happen
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "message_end":
                message = event.get("message", {})
                if message.get("role") == "assistant":
                    text = _text_of(message)
                    if text:
                        result.text = text   # last assistant message wins
                    usage = message.get("usage", {}) or {}
                    turn = _context_tokens(usage)
                    result.tokens += turn
                    result.usage.merge(_turn_usage(usage, turn))
                    # Occupancy is read off the last VALID assistant turn, the
                    # way pi does it — an aborted or errored turn reports usage
                    # you can't trust, so it must not overwrite a good reading.
                    if turn and message.get("stopReason") not in ("aborted", "error"):
                        result.context_tokens = turn
                    result.cost += (usage.get("cost", {}) or {}).get("total", 0.0) or 0.0
            if on_event:
                on_event(event)

    stderr = deadline.drain(process.stderr)
    result.returncode = deadline.wait()
    if on_exit:
        on_exit(process.pid)
    if deadline.fired:
        # Checked before the returncode: a terminated child exits non-zero, so
        # without this a timeout would read as an ordinary "pi exited -15" and
        # say nothing about the factory having ended it. Whatever the agent did
        # emit is already in raw_output.jsonl, and the partial result rides
        # along on the exception: pi reports usage per message, so a turn that
        # hung after real work has already been billed for it.
        raise AgentTimeout(deadline.reason(NAME, request.raw_output_path), result)
    if result.returncode != 0 and not result.text:
        raise RuntimeError(f"pi exited {result.returncode}: {stderr.strip()[-800:]}")
    return result
