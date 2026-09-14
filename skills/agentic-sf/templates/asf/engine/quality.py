"""Deterministic lint, typecheck, build, and test blocks.

A known command is not a judgement call. Anything whose invocation you can write
down belongs here as code — it runs in milliseconds, costs nothing, and returns
the same answer every time. Agents are for the parts that need reading and
deciding.

╔══════════════════════════════════════════════════════════════════════════════╗
║  REPLACE THE PLACEHOLDER COMMANDS BELOW.                                     ║
║                                                                              ║
║  A block that has not been wired up FAILS. It runs nothing, spawns nothing,   ║
║  and reports exit 78 with the line you are reading — because the alternative  ║
║  is a chain that reports a green suite it never ran, and then a pull request  ║
║  that says "tests pass". An unwired block is a missing answer, and a missing  ║
║  answer is not a passing one.                                                ║
║                                                                              ║
║  A stamped repo has no way to guess your test runner; `install.py` tries      ║
║  (bun.lock, pyproject.toml, package.json scripts) and writes in what it       ║
║  finds, so some of the blocks below may already name a real command.          ║
║  `just doctor` lists whichever are still unwired.                             ║
║                                                                              ║
║  For each block you want: swap `_placeholder(...)` for the real argv, e.g.    ║
║      argv=["bun", "test", "apps/web/server.test.ts"]                         ║
║      argv=["uv", "run", "pytest", "-q"]                                      ║
║      argv=["npm", "run", "lint"]                                             ║
║  Delete the blocks you don't need, and drop them from run_quality()'s list.   ║
║                                                                              ║
║  Two rules when you write the real command:                                  ║
║    1. argv LIST, never a shell string — no quoting bugs, no shell injection.  ║
║    2. Call binaries by BARE NAME. These blocks inherit the operator's         ║
║       environment (see utils.operator_env), so `bun`, `uv`, `pytest` resolve  ║
║       exactly as they do in their terminal. Never hard-code an absolute path  ║
║       like /Users/you/.bun/bin/bun — that bakes your machine into the trace.  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import inspect
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable

from .data_types import (EventRecord, QualityCheckResult, QualityCheckSpec, QualityResult,
                         VerifyOutput)
from .utils import now_iso, operator_env

# How much of a failing command's output rides back inside the envelope. Enough
# for a builder to act on without opening the artifact; bounded so a runaway
# stack trace can't swamp the next agent's context.
TAIL_CHARS = 4_000


# Not a command — a marker. `_run` recognises it and fails the check without
# spawning anything, which is the whole point: there is no shell string that
# both fails loudly and cannot be mistaken for a real invocation in the trace.
UNWIRED = "__asf_unwired__"
EXIT_UNWIRED = 78                  # sysexits.h EX_CONFIG: the configuration is wrong


def _placeholder(name: str) -> list[str]:
    """The argv of a block nobody has wired up yet. Replace every call to this.

    It used to be an `echo` that exited 0, and a stamped repo therefore shipped
    with a test phase that passed without testing anything — the most expensive
    default the factory had. Now it fails, and says exactly what to edit.
    """
    return [UNWIRED, name]


# One example per block, so the fix reads like the thing it is asking for. A
# `lint` block told to look like `bun test` is a fix nobody follows literally.
EXAMPLES = {
    "test": '["bun", "test"] or ["uv", "run", "pytest", "-q"]',
    "lint": '["npm", "run", "lint"] or ["uv", "run", "ruff", "check", "."]',
    "typecheck": '["bunx", "tsc", "--noEmit"] or ["uv", "run", "mypy", "."]',
    "build": '["npm", "run", "build"] or ["cargo", "build"]',
}


def _unwired_message(name: str) -> str:
    """What a block that was never wired up reports. Rides back in the envelope,
    so the builder reads it too — hence the fix rather than just the complaint."""
    example = EXAMPLES.get(name, '["your", "command", "here"]')
    return (f"No {name} command is wired up. asf/engine/quality.py ships this "
            f"block as a placeholder, and a placeholder FAILS rather than passing: a "
            f"green suite that never ran is worse than a red one.\n\n"
            f"Fix: open asf/engine/quality.py and replace "
            f"`_placeholder(\"{name}\")` with the argv that {name}s this repo — "
            f"{example} — or delete the block and drop it from BLOCKS there.")


def placeholders() -> list[str]:
    """Which blocks still carry the shipped placeholder instead of a command.

    Read off the source of the blocks themselves rather than a list somebody
    has to remember to update: a block whose argv you replaced no longer
    mentions `_placeholder`, and that IS the answer. `preflight.quality()` and
    `just doctor` are the callers.
    """
    unwired = []
    for name, block in BLOCKS.items():
        try:
            source = inspect.getsource(block)
        except OSError:                      # a block defined outside a file
            continue
        if "_placeholder(" in source:
            unwired.append(name)
    return unwired


def _check_dir(run, name: str) -> Path:
    seq = run.phases[-1].seq if run.phases else 0
    path = run.context_handoff_dir / "quality" / f"{seq:02d}_{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(spec: QualityCheckSpec, run) -> QualityCheckResult:
    phase = run.phases[-1]
    output_dir = _check_dir(run, spec.name)
    output_artifact = output_dir / "command.log"
    # An unwired block is a check that cannot be performed, so it is reported as
    # a failure — never spawned, never passed. Everything after this branch is
    # identical for both paths on purpose: artifact, trace event, console line
    # and envelope, so an unwired block reaches the builder through exactly the
    # same door a red test suite does.
    unwired = spec.argv[:1] == [UNWIRED]
    command = (f"<unwired {spec.name} block — asf/engine/quality.py>"
               if unwired else shlex.join(spec.argv))

    run.console.note(f"quality {spec.name}: {command}")
    started_at = now_iso()
    clock = time.monotonic()
    stdout = ""
    stderr = ""
    if unwired:
        returncode = EXIT_UNWIRED
        stderr = _unwired_message(spec.name)
    else:
        try:
            completed = subprocess.run(
                spec.argv,
                cwd=run.repo_root,
                env=operator_env(),   # the engineer's own shell environment
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
            )
            returncode = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as error:
            returncode = 124
            stdout = error.stdout or ""
            stderr = (error.stderr or "") + f"\nTimed out after {spec.timeout_seconds}s."
        except OSError as error:
            # A missing binary lands here as exit 127 with the real message — no
            # pre-flight probe needed, and none wanted.
            returncode = 127
            stderr = str(error)

    duration = time.monotonic() - clock
    output_artifact.write_text(
        f"$ {command}\nexit: {returncode}\nduration_seconds: {duration:.3f}\n"
        f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"
    )
    passed = returncode == 0
    run.tracer.event(EventRecord(
        adw_id=run.adw_id,
        phase_id=phase.phase_id,
        type="tool_call",
        name=f"quality:{spec.name}",
        payload={
            "area": spec.area,
            "operation": spec.operation,
            "command": command,
            "returncode": returncode,
            "passed": passed,
            "output_artifact": str(output_artifact),
        },
        started_at=started_at,
        ended_at=now_iso(),
    ))
    run.console.note(
        f"quality {spec.name}: {'passed' if passed else 'failed'} "
        f"(exit {returncode}, {duration:.1f}s)"
    )
    return QualityCheckResult(
        name=spec.name,
        area=spec.area,
        operation=spec.operation,
        command=command,
        returncode=returncode,
        passed=passed,
        duration_seconds=duration,
        output_artifact=str(output_artifact),
        output_tail=(stdout + stderr)[-TAIL_CHARS:],
    )


# ── Blocks ────────────────────────────────────────────────────────────────────
# Replace every argv below. See the banner at the top of this file.

def test(run) -> QualityCheckResult:
    """Run the project's test suite. The highest-value block to wire up first."""
    return _run(QualityCheckSpec(
        name="test",
        area="backend",
        operation="build",
        argv=_placeholder("test"),        # e.g. ["bun", "test"] or ["uv", "run", "pytest", "-q"]
        timeout_seconds=600,
    ), run)


def lint(run) -> QualityCheckResult:
    return _run(QualityCheckSpec(
        name="lint",
        area="backend",
        operation="lint",
        argv=_placeholder("lint"),        # e.g. ["bun", "x", "oxlint@1.36.0", "src"]
    ), run)


def typecheck(run) -> QualityCheckResult:
    return _run(QualityCheckSpec(
        name="typecheck",
        area="backend",
        operation="typecheck",
        argv=_placeholder("typecheck"),   # e.g. ["bun", "x", "tsc", "--noEmit"]
    ), run)


def build(run) -> QualityCheckResult:
    # Where a wired-up build writes its bundle, created here so the argv below
    # can simply point at it. `_check_dir` makes the check's own directory; the
    # bundle subdirectory under it is this block's, and a build tool that will
    # not create its own --outdir would otherwise fail on a path that does not
    # exist yet.
    output_dir = _check_dir(run, "build") / "bundle"
    output_dir.mkdir(parents=True, exist_ok=True)
    return _run(QualityCheckSpec(
        name="build",
        area="backend",
        operation="build",
        argv=_placeholder("build"),       # e.g. ["bun", "build", "src/index.ts", "--outdir", str(output_dir)]
    ), run)


# Every block this file offers, by name. `run_quality` runs them, `placeholders`
# inspects them, and `just doctor` reports them — one list, so a block added or
# deleted here cannot be half-registered.
BLOCKS: dict[str, Callable] = {
    "test": test,
    "lint": lint,
    "typecheck": typecheck,
    "build": build,
}


def run_blocks(run, names: list[str]) -> QualityResult:
    """The named blocks, in order, as one QualityResult — what a `verify` stage
    runs. A name that is not in BLOCKS is refused here, before anything spawns,
    because a workflow that asks for a block this repository never wired up is
    a configuration error and not a red suite.
    """
    unknown = [name for name in names if name not in BLOCKS]
    if unknown:
        raise RuntimeError(f"quality: no such block(s) {unknown} — this repo's "
                           f"asf/engine/quality.py offers {sorted(BLOCKS)}")
    checks = [BLOCKS[name](run) for name in names]
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(passed=not failures, checks=checks, failures=failures,
                         artifacts=[check.output_artifact for check in checks])


def run_tests(run) -> QualityResult:
    """The test suite alone, as a QualityResult — the deterministic test phase.

    This is what replaces a `tester` agent once the command is written down. An
    agent rediscovering the runner on every run costs a fortune to learn what a
    subprocess already knows; the repair loop is unchanged, because a failure
    still reaches the builder through `as_envelope` below.
    """
    check = test(run)
    failures = ([] if check.passed else
                [f"{check.name}: `{check.command}` exited {check.returncode}\n"
                 f"{check.output_tail}".rstrip()])
    return QualityResult(passed=check.passed, checks=[check], failures=failures,
                         artifacts=[check.output_artifact])


def as_envelope(result: QualityResult, what: str) -> VerifyOutput:
    """Wrap a deterministic result so an agent can be handed it directly.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door an agent's report would — the ADW
    script is the only thing that knows the difference.
    """
    return VerifyOutput(
        status="success" if result.passed else "fail",
        summary=(f"{what}: all {len(result.checks)} check(s) passed" if result.passed
                 else f"{what}: {len(result.failures)} of {len(result.checks)} check(s) failed"),
        artifacts=result.artifacts,
        notes_for_next_agent=("" if result.passed else
                              "Fix every failure below. The output is verbatim from the "
                              "command — trust it over any summary."),
        passed=result.passed,
        failures=result.failures,
    )


def run_quality(run) -> QualityResult:
    """Run every block and collect ALL failures — one pass tells you everything.

    Ordering contract for the caller: a failing block does NOT fail the phase.
    The runner did its job; the CODE is what failed. Hand this result to the
    builder and let the bounded repair loop decide the run's fate.

    Which blocks run is `BLOCKS` above — delete the ones this repo has no use
    for there, in the one place `placeholders()` and `just doctor` also read.
    """
    checks = [block(run) for block in BLOCKS.values()]
    # A failure is the command, its exit code, and what it actually printed —
    # everything a builder needs to repair without opening a log or being told
    # what the error "means" by a parser that guessed.
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        artifacts=[check.output_artifact for check in checks],
    )
