"""Everything that would fail later, asked now.

WHY THIS EXISTS. A factory run is cheap to start and expensive to lose. The
failures that hurt most are not the interesting ones — they are a missing
provider key, a `base_ref` that does not exist, a `data_dir` nothing can write
to. Every one of them is knowable in milliseconds, and every one of them used
to surface partway into a chain, after a planner had already been paid for.

So the checks live here, in one module, and they are asked in two places:

  * `before_run(cfg)` — the cheap, unconditional subset, called by
    `session.ensure()` before the worktree, the session's own `run.json` or a
    process record exists. A fatal finding aborts while the repo is untouched.
  * `everything(cfg)` — the full sweep, which is what `just doctor` prints.
    It includes the checks that are too slow, too situational or too noisy to
    put in front of every run: harness reachability, the forge CLI, the quality
    blocks, the trace UI's port.

Two rules for anything added here:

1. **A check that cannot be sure says `warn`, never `fatal`.** Refusing to
   start on a guess is worse than the failure it was guessing about. The
   credential check is the live example: pi's provider-to-key mapping is
   authoritative only when `models.json` declares it.
2. **Every finding carries its `fix`.** Naming a problem without naming the
   command that ends it just moves the search earlier; the point of asking now
   is that the answer is actionable while nothing has been spent.

Nothing here knows what a phase is, nothing here writes to the trace, and
nothing here READS the trace db — the same rule the rest of the factory follows
(`tracer.py`). Every answer comes from the config, the filesystem, the
environment, or the harness itself, so these checks work identically on a repo
whose db has been deleted and on one whose events go somewhere else entirely.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

from . import git_helper, harnesses
from .data_types import AgentConfig, Finding, FactoryConfig
from .utils import anchor

# The trace UI's two ports, mirrored from scripts/up.py — checked here so
# `doctor` and `up` answer the same question the same way.
API_PORT = int(os.environ.get("PORT", "4600"))


# ── git: the tree a run cuts from ────────────────────────────────────────────

def repo(cfg: FactoryConfig, main_root: Path) -> list[Finding]:
    """Whether this checkout can do what the config says runs will do.

    None of it is fatal on its own — `worktree.ensure()` falls back to running
    in place when there is no repository and no commit, which is a legitimate
    way to run a read-only chain. It is fatal the moment a phase tries to
    commit, and that is an hour later, so it is said out loud now.
    """
    findings: list[Finding] = []
    if not git_helper.is_repo(main_root):
        return [Finding(
            check="git", level="warn",
            detail=f"not a git repository — runs work directly in {main_root}, with "
                   f"no worktree and no branch",
            fix="git init && git commit --allow-empty -m init — without one, a commit "
                "or integrate phase raises")]

    if not git_helper.ref_exists(main_root, "HEAD"):
        findings.append(Finding(
            check="git", level="warn",
            detail="the repository has no commit yet, so there is nothing to branch "
                   "from — runs work directly in this checkout",
            fix="git commit --allow-empty -m init"))
        return findings

    # A configured base_ref that does not resolve is the one git problem that IS
    # fatal: `git worktree add <path> <branch> <base>` fails with a raw git
    # error and the run dies before its first phase, having already minted a
    # session. Better to say which ref, and where it is configured.
    base_ref = cfg.worktree.base_ref
    if cfg.worktree.enabled and base_ref and not git_helper.ref_exists(main_root, base_ref):
        findings.append(Finding(
            check="git", level="fatal",
            detail=f"worktree.base_ref is {base_ref!r}, which does not resolve to a "
                   f"commit in this checkout",
            fix=f"fetch or create {base_ref!r}, or clear worktree.base_ref in the "
                f"config to cut from whatever main has checked out"))
        return findings

    # A check that says nothing when it passes reads as a check that never ran,
    # which in a report is worse than noise — so the good news is a line too.
    cut_from = base_ref or git_helper.current_branch(main_root)
    findings.append(Finding(
        check="git",
        detail=(f"runs branch from {cut_from} into {cfg.worktree.dir}/<adw_id>"
                if cfg.worktree.enabled else
                f"worktree.enabled is false — runs work directly in {main_root}")))
    return findings


# ── runtime: where the record is written ─────────────────────────────────────

def runtime(cfg: FactoryConfig, main_root: Path) -> list[Finding]:
    """Whether the session's own directory, and the trace mirror, can be written.

    Anchored to the MAIN checkout, exactly as `session.ensure` anchors them, so
    this asks the same question about the same directories. `data_dir` is the
    one that matters: it holds `sessions/<adw_id>/`, and with it `run.json`,
    `processes.jsonl`, `context_handoff/` and every envelope — the record every
    other command in the factory reads back (`artifacts.py`). Unwritable, the
    session dies at its first event, after the first agent has been spawned.

    The db's directory is checked for the same reason and no other: `Tracer`
    opens the file at session start, so it has to exist and be writable for a
    run to begin. **Nothing here opens the db, and nothing in the factory ever
    reads it** — it is a write-only mirror (see `tracer.py`), and the day the
    events go to a hosted API this check is the only line that has to go.

    Both are probed by creating the directory if missing and writing a file that
    is then removed — the same two things the run itself would do a moment
    later, which is the only way to answer honestly. Nothing else is touched.
    """
    findings: list[Finding] = []
    for label, target in (("data_dir", anchor(main_root, cfg.defaults.data_dir)),
                          ("trace mirror", anchor(main_root, cfg.observability.db).parent)):
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".asf-write-probe"
            probe.write_text("")
            probe.unlink()
        except OSError as error:
            findings.append(Finding(
                check="runtime", level="fatal",
                detail=f"{label} {target} cannot be written: {error}",
                fix=f"fix the permissions on {target}, or point "
                    f"{'defaults.data_dir' if label == 'data_dir' else 'observability.db'} "
                    f"somewhere writable"))
    if findings:
        return findings
    return [Finding(check="runtime",
                    detail=f"the record is written to {anchor(main_root, cfg.defaults.data_dir)}")]


# ── credentials: the key the model needs ─────────────────────────────────────

def credentials(agent: AgentConfig) -> list[Finding]:
    """Whether this agent's harness can authenticate, asked of the harness.

    `agents.validate()` checks that a model is WRITTEN correctly. This is the
    other half — that the credential behind it exists — and it is the failure
    that used to land mid-chain, because a provider only complains when it is
    called. A harness that does not implement `credentials` (its CLI brings its
    own auth, as Claude Code's does) contributes nothing here.
    """
    driver = harnesses.HARNESSES.get(agent.harness)
    ask = getattr(driver, "credentials", None)
    return list(ask(agent)) if ask else []


def roster(cfg: FactoryConfig) -> list[Finding]:
    """Every agent in the config: is its harness there, and can it authenticate."""
    findings: list[Finding] = []
    for name in sorted({agent.harness for agent in cfg.agents}):
        driver = harnesses.HARNESSES.get(name)
        if driver is None:
            findings.append(Finding(
                check=f"harness: {name}", level="fatal",
                detail=f"{name!r} is not one of {' | '.join(harnesses.NAMES)}",
                fix="fix `harness:` in the roster, or add the harness — "
                    "references/harnesses.md"))
            continue
        try:
            driver.reachable()
            findings.append(Finding(check=f"harness: {name}",
                                    detail="CLI reachable"))
        except RuntimeError as error:
            findings.append(Finding(
                check=f"harness: {name}", level="fatal", detail=str(error),
                fix="install that CLI and put it on PATH"))
    for agent in cfg.agents:
        findings += credentials(agent)
    return findings


# ── quality: the blocks that decide whether code is done ─────────────────────

def quality(main_root: Path) -> list[Finding]:
    """Which quality blocks are still unwired placeholders.

    The single most expensive default in the factory, which is why it is asked
    twice: the block itself fails when it runs (see `quality.py`), and doctor
    says so before anything runs at all.
    """
    try:
        from . import quality as quality_module
        unwired = quality_module.placeholders()
    except Exception as error:                       # a hand-edited quality.py
        return [Finding(check="quality", level="warn",
                        detail=f"could not inspect asf/engine/quality.py: {error}",
                        fix="open it and confirm every block names a real command")]
    if not unwired:
        return [Finding(check="quality", detail="every block names a real command")]
    return [Finding(
        check="quality", level="warn",
        detail=f"{len(unwired)} block(s) are still placeholders: {', '.join(unwired)}",
        fix="replace the `_placeholder(...)` argv in asf/engine/quality.py with "
            "this repo's real commands, and delete the blocks you do not want. A "
            "placeholder FAILS its phase rather than passing it")]


# ── the forge CLI: what the watchers and integration shell out to ────────────

# `gh pr edit` and `gh issue edit` asked for `projectCards` on every edit until
# this release, and GitHub now answers that field with a hard error — so on
# anything older EVERY label edit fails, whatever the edit was for. That is not
# cosmetic: the label is how both watchers mark a run's outcome, and a `failed`
# that cannot be applied means the next poll finds the same work and buys the
# same failing run again. cli/cli#13282, released in gh 2.98.0.
GH_LABELS_FIXED = (2, 98)


def _gh_version(binary: str) -> tuple[int, ...]:
    """(major, minor) of the CLI's own `--version`, or () when it cannot be read.

    A QUESTION, not a command — an unreadable answer is not a finding. `gh
    version 2.70.0 (2025-04-11)` is the shape; distribution and enterprise
    builds append to it and are read by the same expression.
    """
    try:
        completed = subprocess.run([binary, "--version"], capture_output=True,
                                   text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ()
    found = re.search(r"(\d+)\.(\d+)\.\d+", completed.stdout or "")
    return (int(found.group(1)), int(found.group(2))) if found else ()


def forge(cfg: FactoryConfig) -> list[Finding]:
    """Whether the CLI each enabled forge path needs is on PATH, and can label.

    Only asked about the paths this repository actually turned on. A repo that
    integrates with `mode: merge` and has both watchers off needs no forge CLI
    at all, and telling it about `gh` would be noise.

    THE VERSION IS ASKED BECAUSE PRESENCE IS NOT ENOUGH. An authenticated `gh`
    on PATH that cannot move a label passes every check this used to make, and
    fails the one thing the watchers need it for. `before_run` says a CLI version
    probe belongs here rather than in front of every run, which is where it is.
    """
    wanted: dict[str, list[str]] = {}
    integration = cfg.worktree.integration
    if integration.mode == "pr" and integration.open_pr and integration.pr_command:
        wanted.setdefault(integration.pr_command[0], []).append("worktree.integration.open_pr")
    if cfg.issues.enabled and cfg.issues.list_command:
        wanted.setdefault(cfg.issues.list_command[0], []).append("issues.enabled")
    if cfg.pull_requests.enabled and cfg.pull_requests.list_command:
        wanted.setdefault(cfg.pull_requests.list_command[0], []).append("pull_requests.enabled")

    labels_wanted = (cfg.issues.enabled and cfg.issues.state_command) or \
                    (cfg.pull_requests.enabled and cfg.pull_requests.state_command)

    findings: list[Finding] = []
    for binary, wanted_by in sorted(wanted.items()):
        if shutil.which(binary):
            findings.append(Finding(check=f"forge: {binary}",
                                    detail=f"on PATH, needed by {', '.join(wanted_by)}"))
            version = _gh_version(binary) if binary == "gh" and labels_wanted else ()
            if version and version < GH_LABELS_FIXED:
                findings.append(Finding(
                    check=f"forge: {binary}", level="warn",
                    detail=f"gh {version[0]}.{version[1]} cannot move a label: "
                           f"`gh pr edit` and `gh issue edit` still ask for Projects "
                           f"(classic), which GitHub answers with an error, so every "
                           f"edit fails. The watchers mark a run's outcome with a label "
                           f"— unmarked, a failed run is relaunched on the next poll",
                    fix=f"upgrade gh to {GH_LABELS_FIXED[0]}.{GH_LABELS_FIXED[1]} or "
                        f"newer (`brew upgrade gh`, or your package manager)"))
        else:
            findings.append(Finding(
                check=f"forge: {binary}", level="warn",
                detail=f"{binary!r} is not on PATH, but {', '.join(wanted_by)} needs it",
                fix=f"install {binary} and authenticate it, or turn that path off in "
                    f"the config. A watcher without it polls forever and lists nothing"))
    return findings


# ── the skill, and the UI that ships with it ─────────────────────────────────

def skill() -> list[Finding]:
    """Whether ASF_SKILL still points at the agentic-sf skill directory.

    install.py writes it into .env, and a `.env` that travelled to another
    machine with the repo is the way this goes wrong — the path is real on
    somebody's laptop and absent here.
    """
    raw = os.environ.get("ASF_SKILL", "").strip()
    if not raw:
        return [Finding(
            check="ASF_SKILL", level="warn",
            detail="unset — `just up` and `just obs` start without the trace UI, and "
                   "re-installing or upgrading the factory cannot find the skill",
            fix="re-run install.py from the target repo root; it writes the path "
                "into .env. A repo cloned without its (gitignored) .env lands here")]
    root = Path(raw).expanduser()
    if not (root / "scripts" / "install.py").is_file():
        return [Finding(
            check="ASF_SKILL", level="warn",
            detail=f"{raw} does not look like the agentic-sf skill (no scripts/install.py) — "
                   f"a .env from another machine does exactly this",
            fix="set ASF_SKILL in .env to this machine's skill directory, or "
                "re-run install.py from the target repo root")]
    return [Finding(check="ASF_SKILL", detail=str(root))]


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def visualizer_dir() -> Path | None:
    """Where the trace UI lives, or None when this repo cannot reach it.

    The visualizer ships with the SKILL, not with the stamp, so both `up` and
    `doctor` find it through the `ASF_SKILL` the installer wrote into `.env` —
    and a repo cloned without that (gitignored) `.env` cannot. It lives here,
    not in `supervise.py`, so that `doctor` asks the identical question `up`
    acts on; asking a cheaper one is how doctor came to print a ✓ for a UI that
    could not start.
    """
    skill_root = os.environ.get("ASF_SKILL", "").strip()
    if not skill_root:
        return None
    home = Path(skill_root).expanduser() / "apps" / "visualizer"
    return home if (home / "server" / "index.ts").is_file() else None


def trace_ui() -> list[Finding]:
    """Whether `just up` / `just obs` could start the trace UI over the trace db.

    Reachability first: `up` drops the UI and keeps going whenever the
    visualizer cannot be found, and a green check that only means "bun is
    installed" points the operator away from the one thing that is wrong.
    """
    home = visualizer_dir()
    if home is None:
        raw = os.environ.get("ASF_SKILL", "").strip()
        return [Finding(
            check="trace UI", level="warn",
            detail=("ASF_SKILL is unset, so `just up` and `just obs` start without the "
                    "trace UI — the watchers run, the UI is silently skipped") if not raw
                   else (f"no visualizer under {Path(raw).expanduser() / 'apps' / 'visualizer'} "
                         f"— ASF_SKILL does not point at the skill directory, so `just up` "
                         f"and `just obs` start without the trace UI"),
            fix="re-run install.py from the target repo root, or set ASF_SKILL in .env to "
                "the skill directory (the visualizer is its apps/visualizer)")]
    if not shutil.which("bun"):
        return [Finding(check="trace UI", level="warn",
                        detail="bun is not on PATH — `just obs` cannot start the trace UI",
                        fix="install bun (https://bun.sh), or run without the UI")]
    if not port_free(API_PORT):
        return [Finding(
            check="trace UI", level="warn",
            detail=f"something already listens on :{API_PORT} — another trace UI, or "
                   f"an api server orphaned by an older `just obs`",
            fix=f"lsof -ti :{API_PORT} | xargs kill")]
    return [Finding(check="trace UI", detail=f"{home}, bun present, :{API_PORT} free")]


# ── composition ──────────────────────────────────────────────────────────────

def before_run(cfg: FactoryConfig, main_root: Path | None = None) -> list[Finding]:
    """The subset every run is worth paying for. Raises SystemExit on a fatal.

    Deliberately small and deliberately fast: git questions the run is about to
    ask anyway, and one write probe. Everything slower — a CLI version probe, a
    port bind, a forge lookup — belongs to `doctor`, because a check that adds a
    second to every run is a check people turn off.

    Returns the warnings, for the caller to surface once it has a console.
    """
    root = Path(main_root) if main_root else git_helper.main_root()
    findings = repo(cfg, root) + runtime(cfg, root)   # ok findings are dropped below
    fatal = [finding for finding in findings if finding.level == "fatal"]
    if fatal:
        raise SystemExit("preflight failed:\n" + "\n".join(
            f"- {finding.line}\n  fix: {finding.fix}" for finding in fatal))
    return [finding for finding in findings if finding.level == "warn"]


def everything(cfg: FactoryConfig, main_root: Path | None = None) -> list[Finding]:
    """Every check there is, ordered the way an engineer would read them."""
    root = Path(main_root) if main_root else git_helper.main_root()
    return (repo(cfg, root) + runtime(cfg, root) + roster(cfg) + quality(root)
            + forge(cfg) + skill() + trace_ui())
