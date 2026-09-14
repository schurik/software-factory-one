"""The roster as files and folders: `asf/factory.yaml` plus `asf/agents/<name>/`.

factory.yaml is the manifest — defaults, budget, gates, where the trace goes,
how a run's worktree is cut and landed. It holds NO agents. An agent is a
directory with one file, `agent.md`: YAML frontmatter for what the machinery
needs (model, thinking, tools, writes, purpose) and, below it, the prose that
says who the agent is. Its name is the directory's name, so it cannot be
misspelt in two places, and its task is not here at all — that belongs to the
stage that calls it (`engine.tasks`).

What this module produces is the same `FactoryConfig` the rest of the engine has
always run on, so nothing downstream — session, worktree, permissions, the
trace — knows the roster changed shape.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import agents, frontmatter
from .data_types import FactoryConfig

DEFAULT_CONFIG = "asf/factory.yaml"
AGENT_FILE = "agent.md"
AGENT_RESERVED = ("name", "prompt_engineering")


def root_of(config_path: str | Path) -> Path:
    """The `asf/` directory a config lives in — where agents, stages and
    workflows are found relative to it."""
    return Path(config_path).parent


def load(config_path: str | Path = DEFAULT_CONFIG) -> FactoryConfig:
    """factory.yaml + every agents/<name>/agent.md, merged over defaults."""
    path = Path(config_path)
    if not path.is_file():
        raise SystemExit(f"no config at {path} — is the factory installed here?")
    raw = yaml.safe_load(path.read_text()) or {}
    if "agents" in raw:
        raise SystemExit(f"{path}: `agents:` does not belong in factory.yaml — an agent is "
                         f"a directory under {root_of(path) / 'agents'} with an "
                         f"{AGENT_FILE} in it")
    default_harness = (raw.get("defaults") or {}).get("harness", "")
    raw["agents"] = [_agent_entry(directory, default_harness)
                     for directory in agent_dirs(root_of(path))]
    return agents.merge_defaults(raw)


def agent_dirs(root: Path) -> list[Path]:
    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        raise SystemExit(f"no agents directory at {agents_dir} — is the factory installed?")
    found = sorted(p for p in agents_dir.iterdir() if p.is_dir())
    if not found:
        raise SystemExit(f"{agents_dir} holds no agent — every agent is a directory with "
                         f"an {AGENT_FILE} in it")
    return found


def _agent_entry(directory: Path, default_harness: str) -> dict:
    """One agent.md → one raw roster entry.

    The frontmatter is the entry; the body is the identity. The engine hands
    `prompt_engineering.system` the same file, and `prompts.render` strips the
    frontmatter again on the way to the model — so the file is read twice, by
    two readers, and each sees only its half.
    """
    spec = directory / AGENT_FILE
    if not spec.is_file():
        raise SystemExit(f"agent {directory.name!r}: {spec} is missing")
    entry, identity = frontmatter.split(spec.read_text(), str(spec))
    if not entry:
        raise SystemExit(f"agent {directory.name!r}: {spec} has no frontmatter — it opens "
                         f"with a `---` block holding purpose, thinking, writes, …")
    reserved = [key for key in AGENT_RESERVED if key in entry]
    if reserved:
        raise SystemExit(f"agent {directory.name!r}: {spec} sets {reserved} — the name is "
                         f"the directory's and the identity is the prose below the "
                         f"frontmatter")
    harness = entry.get("harness", default_harness)
    # A harness-specific identity wins over the neutral one, because the two
    # differ in what they may tell the agent to do (pi's planner fans out to
    # subagent tools; Claude Code has none). It is prose only: the frontmatter
    # stays in agent.md, one boundary per agent whatever runs it. The task
    # files never carry that difference — they describe the job, not the tools.
    system = directory / f"agent.{harness}.md"
    if system.is_file():
        if frontmatter.split(system.read_text(), str(system))[0]:
            raise SystemExit(f"agent {directory.name!r}: {system.name} carries frontmatter "
                             f"— the config lives in {AGENT_FILE}; a harness file is the "
                             f"identity only")
        identity = system.read_text()
    else:
        system = spec
    if not identity.strip():
        raise SystemExit(f"agent {directory.name!r}: {system} says nothing below the "
                         f"frontmatter — who is this agent?")
    entry["name"] = directory.name
    entry["prompt_engineering"] = {"system": str(system), "user": ""}
    return entry
