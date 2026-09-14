"""Human-in-the-loop gates: stop after a phase, ask a person, continue or revise.

A gate is its own `kind="engineer"` phase — the lane that until now only ever
logged the request — plus a revise loop that is the reviewer loop in
the review stage with a person where the reviewer is. `gated()` owns both.
An ADW spends one call per gate and never sees the loop, the wait, or the file.

THE WAIT IS A SUSPEND. `decide()` looks for a decision this session already
recorded for the gate and round; finding none, it records what it is waiting
for in `run.json`, exits the process with status 75, and leaves the worktree
where it is. `just approve <adw_id>` writes the decision and re-launches the
same workflow with `--resume`: replay answers every recorded agent phase from
the record, the chain reaches the gate again, and this time the decision is
there. A terminal prompt is a convenience over that — while a person is at THIS
RUN's terminal the run asks in place and polls the same file, and `d` or
`wait_seconds` turns the block into the suspend it would have been anyway. A
watcher's terminal, inherited by the run it launched, is not that terminal;
`attended()` says how the two are told apart.

THE DECISION NAMES WHAT IT DECIDED. `subject_digest` hashes the artifact files
at the moment the human was asked; a decision whose digest does not match the
subject in front of the run now is refused and the run waits again. That is
the one rule everything here rests on, and it is what makes a decision file
safe to write from anywhere.

TRUST IS RECORDED. A gate the policy skips writes `verdict=approve,
by="policy", channel="auto"` to the same directory, so the record of a run
shows every gate it passed and who passed it.

Files only. The trace db mirrors the events; nothing here reads it.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import artifacts
from .data_types import (Decision, EnvelopeBase, EventRecord, Gate, HitlConfig,
                         Phase, PhaseParams, Subject, WaitingFor)
from .utils import now_iso

EXIT_WAITING = 75          # EX_TEMPFAIL: "try again later", which is exactly it
POLL_SECONDS = 2.0         # attended: how long one look at the keyboard waits


# ── the record ───────────────────────────────────────────────────────────────

def digest(paths: list[Path]) -> str:
    """One hash over the subject's files, independent of listing order.

    A missing file hashes as its name plus a marker, so "the plan is gone" is a
    different subject from "the plan is here" and from "there was no plan".
    """
    hasher = hashlib.sha256()
    for path in sorted(Path(p) for p in paths):
        hasher.update(str(path).encode())
        hasher.update(b"\0")
        try:
            hasher.update(path.read_bytes())
        except OSError:
            hasher.update(b"<missing>")
        hasher.update(b"\0")
    return hasher.hexdigest()


def decision_path(session_dir: Path, gate: str, round: int) -> Path:
    return artifacts.decisions_dir(session_dir) / f"{gate}_{round}.json"


def record(session_dir: Path, decision: Decision) -> Path:
    """Write one decision. Keyed by gate and round; a later write replaces."""
    path = decision_path(session_dir, decision.gate, decision.round)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(decision.model_dump_json(indent=2))
    return path


def read_decision(session_dir: Path, gate: str, round: int) -> Optional[Decision]:
    path = decision_path(session_dir, gate, round)
    if not path.is_file():
        return None
    try:
        return Decision(**json.loads(path.read_text()))
    except (ValueError, OSError):
        return None            # a half-written file is a missing answer, not a crash


# ── the keyboard ─────────────────────────────────────────────────────────────

UNATTENDED_ENV = "ASF_UNATTENDED"    # a launcher saying "my terminal is not this run's"


def attended() -> bool:
    """Whether anyone can answer at THIS RUN's terminal. False under cron, a watcher, a test.

    A TTY test alone is not enough, because a TTY can be INHERITED. `issue_watch`
    and `pr_watch` launch a chain with a blocking `subprocess.run` that passes
    their own stdin straight through, so a watcher started by hand from a
    terminal — `just up` in a window someone left open — hands every run it
    starts a keyboard that looks exactly like the engineer's. Asking there
    prompts whoever is watching the queue, about a plan they never asked to
    read, interleaved with the poll log; and because the watchers are
    deliberately serial, it stops the whole queue for `hitl.wait_seconds` (900
    by default) before suspending anyway. Under cron the same run suspends at
    once. Two very different behaviours from one line of config is the bug.

    Only the LAUNCHER knows which it is, so the launcher says so: the watchers
    set `ASF_UNATTENDED` on the runs they start. `run.trigger` cannot answer
    this — an engineer who types `uv run asf/asf.py run issue 42` at their own
    keyboard is on the `issue` trigger too, and should still be asked in place.
    """
    if os.environ.get(UNATTENDED_ENV, "").strip():
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def read_keypress(waiting: WaitingFor) -> tuple[str, str]:
    """The real `ask`: one line from a TTY, or ("", "") after POLL_SECONDS so the
    caller can look at the decision record again. Notes are required on a reject
    — they are what the agent revises from."""
    ready, _, _ = select.select([sys.stdin], [], [], POLL_SECONDS)
    if not ready:
        return "", ""
    key = sys.stdin.readline().strip().lower()[:1]
    if key == "a":
        return "approve", input("notes for the next agent (optional): ").strip()
    if key == "r":
        notes = ""
        while not notes:
            notes = input("what should change: ").strip()
        return "reject", notes
    if key == "x":
        return "abort", input("reason (optional): ").strip()
    if key == "d":
        return "detach", ""
    return "", ""


# ── the policy ───────────────────────────────────────────────────────────────

OVERRIDES = ("all", "none", "every")


class HitlPolicy:
    """Whether a named gate fires, resolved most-specific-first.

    `override` is the `--hitl` flag (or `ASF_HITL`): `all`, `none`, `every`,
    or a comma-separated list of gate names. It is a person saying so at the
    keyboard, and it wins over everything in the config — including
    `when_unattended`, because a flag on an issue run's argv was put there by
    the operator who launched the watcher.

    `ask` is how an attended run asks in place: a callable taking the
    `WaitingFor` and returning `(verdict | "detach" | "", notes)`. None means
    nobody is at THIS RUN's keyboard — see `attended()`, which is not merely a
    TTY test — and the gate suspends at once. It lives here because "is anyone
    attending" is a policy input, and because this is the run-scoped object a
    test can hand a scripted answerer to.
    """

    def __init__(self, config: HitlConfig, override: str = ""):
        self.config = config
        self.override = (override or "").strip().lower()
        self.every = self.override == "every"
        self._named: set[str] = set()
        if self.override and self.override not in OVERRIDES:
            names = {part.strip() for part in self.override.split(",") if part.strip()}
            if not names or any(not part.replace("_", "").isalnum() for part in names):
                raise ValueError(f"--hitl {override!r}: expected all | none | every | "
                                 f"a comma-separated list of gate names")
            self._named = names
        self.ask = read_keypress if attended() else None

    def mode(self, gate: str, trigger: str) -> str:
        """`on` — stop and ask. `auto` — record a policy approval and go on."""
        if self.override in ("all", "every"):
            return "on"
        if self.override == "none":
            return "auto"
        if self._named:
            return "on" if gate in self._named else "auto"
        if not self.config.gates.get(gate, self.config.default):
            return "auto"
        if trigger != "engineer" and self.config.when_unattended == "auto":
            return "auto"
        return "on"

    def summary(self) -> str:
        """The one line the console prints when any gate may fire."""
        source = f"--hitl {self.override}" if self.override else "config"
        on = sorted(gate for gate, value in self.config.gates.items() if value)
        line = f"hitl: {source}"
        if self.every:
            line += " · a checkpoint after every agent phase"
        elif not self.override:
            line += f" · default {'on' if self.config.default else 'off'}"
            if on:
                line += f" · on: {', '.join(on)}"
        return line + (" · attended" if self.ask else " · unattended, gates suspend")


# ── the wait ─────────────────────────────────────────────────────────────────

class Suspended(SystemExit):
    """The run stopped for a human. Exit 75; the record says what it waits for.

    A SystemExit so an ADW's uncaught path is a clean exit with no traceback —
    a run that stopped on purpose must not look like one that crashed — and so
    `Run.phase` can tell it from every other exception and close the phase as
    `waiting` rather than `fail`.
    """

    def __init__(self, waiting: WaitingFor):
        super().__init__(EXIT_WAITING)
        self.waiting = waiting


def resolve_paths(run, paths: list[str]) -> list[Path]:
    """Subject paths as absolute files: absolute stay, relative are the worktree's."""
    return [Path(p) if Path(p).is_absolute() else Path(run.repo_root) / p for p in paths]


def how_to_answer(run, gate: str) -> str:
    return (f"record a Decision with engine.hitl.answer({run.session_dir}, "
            f"'approve'|'reject'|'abort', notes, by), then re-run the workflow with "
            f"--adw-id {run.adw_id} --resume  (a gate CLI is not stamped yet)")


def decide(run, phase: Phase, subject: Subject) -> Decision:
    """The decision for this gate and round — from the record, the terminal, or not yet.

    Order: a recorded decision whose digest matches wins, whoever wrote it. An
    attended run then asks in place, polling the record meanwhile so a
    `just approve` from another terminal is honoured too. Everything else
    suspends. The digest is taken ONCE, here, and every later comparison is
    against it — the human is answering about what they were shown.
    """
    paths = resolve_paths(run, subject.paths)
    fingerprint = digest(paths)
    waiting = WaitingFor(gate=subject.gate, round=subject.round, phase_id=phase.phase_id,
                         phase_name=phase.params.name, since=now_iso(),
                         subject_digest=fingerprint, paths=[str(p) for p in paths],
                         summary=subject.summary, notes=subject.notes)
    run.console.note(f"gate {subject.gate} round {subject.round}: {subject.summary}")
    for path in waiting.paths:
        run.console.note(f"subject: {path}")

    recorded = read_decision(run.session_dir, subject.gate, subject.round)
    if recorded is not None:
        if recorded.consumed_at and run.resuming:
            # This round was already walked by an earlier process, and the
            # chain is re-walking it under --resume. The subject may since have
            # moved on — a reject's revise rewrote the plan in place — so the
            # digest cannot hold and is not asked to: the human already saw
            # this one, and acting on it again is what replay means.
            run.console.note(f"↺ decision {subject.gate}_{subject.round} replayed — "
                             f"{recorded.verdict} by {recorded.by}, already acted on by "
                             f"an earlier process of this session")
            return _consume(run, phase, recorded)
        if recorded.subject_digest == fingerprint:
            return _consume(run, phase, recorded)
        run.console.note(f"decision {subject.gate}_{subject.round} is stale — it decided "
                         f"on a different {subject.gate}; asking again")

    # The blocked-and-polling path. `run.hitl.ask` is None without a TTY; a
    # test injects a scripted answerer the same way a keypress would answer.
    if run.hitl.ask is not None:
        artifacts.update_run(run.session_dir, waiting_for=waiting)   # `just pending` sees it
        answer = _attended(run, waiting)
        if answer is not None:
            return _consume(run, phase, answer)

    _notify(run, waiting)
    raise Suspended(waiting)


def _consume(run, phase: Phase, decision: Decision) -> Decision:
    """Record that a decision was taken, in the trace and by clearing the wait."""
    if not decision.decided_at:
        decision.decided_at = now_iso()
    if not decision.consumed_at:
        decision.consumed_at = now_iso()
    record(run.session_dir, decision)
    artifacts.clear_waiting(run.session_dir)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="decision", name=decision.gate,
                                 payload=decision.model_dump()))
    run.console.decided(decision)
    return decision


def _attended(run, waiting: WaitingFor) -> Optional[Decision]:
    """Ask at the terminal, up to `wait_seconds`, polling the record between keypresses.

    None means suspend — the human detached, or the clock ran out, which is
    the same thing with nobody at the keyboard.
    """
    deadline = time.monotonic() + max(0, run.cfg.hitl.wait_seconds)
    run.console.note("waiting for you — [a]pprove / [r]eject / [x] abort / [d]etach; "
                     "or from another terminal: " + how_to_answer(run, waiting.gate))
    while True:
        recorded = read_decision(run.session_dir, waiting.gate, waiting.round)
        if recorded is not None and recorded.subject_digest == waiting.subject_digest:
            return recorded
        verdict, notes = run.hitl.ask(waiting)
        if verdict == "detach":
            return None
        if verdict in ("approve", "reject", "abort"):
            return Decision(gate=waiting.gate, round=waiting.round, verdict=verdict,
                            notes=notes, by=run.engineer, channel="terminal",
                            subject_digest=waiting.subject_digest)
        if time.monotonic() > deadline:
            run.console.note("no answer within hitl.wait_seconds — suspending")
            return None


def _notify(run, waiting: WaitingFor) -> None:
    """Run `notify_command` with the subject on stdin. Never raises."""
    argv = run.cfg.hitl.notify_command
    if not argv:
        return
    env = {**os.environ, "ASF_ADW_ID": run.adw_id, "ASF_GATE": waiting.gate,
           "ASF_ROUND": str(waiting.round)}
    try:
        subprocess.run(argv, input=waiting.model_dump_json(indent=2), text=True,
                       env=env, timeout=30, capture_output=True)
    except (OSError, subprocess.SubprocessError) as error:
        run.console.note(f"notify_command failed: {error}")


# ── answering from outside the run ───────────────────────────────────────────

def answer(session_dir: Path, verdict: str, notes: str, by: str) -> Decision:
    """A decision from OUTSIDE the run — the CLI. Takes the digest from the wait record.

    A blocked run (attended, still polling) has `waiting_for` set with
    `status == "running"`; a suspended one has it with `status == "waiting"`.
    Both are answered the same way, and the run — polling or resumed — picks
    the file up.
    """
    state = artifacts.read_run(session_dir)
    if state is None or state.waiting_for is None:
        raise RuntimeError(f"{Path(session_dir).name} is not waiting at a gate — "
                           f"`just pending` lists the sessions that are")
    if verdict == "reject" and not notes.strip():
        raise RuntimeError("a reject needs notes (-m \"...\") — they are what the agent "
                           "revises from")
    waiting = state.waiting_for
    decision = Decision(gate=waiting.gate, round=waiting.round, verdict=verdict,
                        notes=notes.strip(), by=by, channel="cli",
                        subject_digest=waiting.subject_digest, decided_at=now_iso())
    record(session_dir, decision)
    return decision


# ── the loop ─────────────────────────────────────────────────────────────────

REVISE_PROMPT = (
    "{prompt}\n\n"
    "The engineer reviewed what you produced and asked for changes — their notes are "
    "`notes_for_next_agent` in previous_envelope, a Decision with verdict \"reject\". "
    "Revise your existing work along those notes in this same session: update the "
    "artifacts you already wrote rather than starting over, keep the same paths, and "
    "report the same Report JSON shape as before."
)


class Aborted(SystemExit):
    """The human ended the run at a gate. Exit 1, with the reason as the message.

    Raised INSIDE the approve phase on purpose: `Run.phase` then closes that
    phase as `fail` with the reason, finalizes the session as `fail`, keeps the
    worktree and prints the banner — exactly what a `GateFailure` gets. Raised
    after the phase closed, the session would read `running` with no process
    behind it, which is the bug `_finalize_when_killed` exists to prevent.
    """


def gated(run, gate: Gate, envelope: EnvelopeBase) -> EnvelopeBase:
    """Stop at a gate, or pass it by policy; loop on reject; return what was approved.

    Phases: `approve_<gate>` (round 1), `approve_<gate>_<n>` (later rounds),
    `<gate>_revise_<n>` between them. Every one replays on `--resume` by its
    name, so a suspended run re-enters the exact round it left.
    """
    if run.hitl.mode(gate.name, run.trigger) == "auto":
        return _pass_by_policy(run, gate, envelope)
    if run.hitl.every and _already_approved(run, gate, envelope):
        # `--hitl every` put a checkpoint of the same name after the phase that
        # produced this envelope, and the human approved there. Asking again
        # would open a second `approve_<gate>` phase for the same subject.
        run.console.note(f"gate {gate.name}: approved at the checkpoint just before")
        return envelope

    round = 1
    while True:
        paths = gate.paths or envelope.artifacts
        name = f"approve_{gate.name}" if round == 1 else f"approve_{gate.name}_{round}"
        description = gate.description or (
            f"Hand the {gate.name} to the engineer and wait for a verdict")
        with run.phase(PhaseParams(name=name, kind="engineer", owner=run.engineer,
                                   description=description)) as ph:
            decision = ph.decide(Subject(gate=gate.name, round=round,
                                         summary=envelope.summary, paths=paths,
                                         notes=envelope.notes_for_next_agent))
            if decision.verdict == "abort":
                raise Aborted(f"aborted by {decision.by} at gate {gate.name}"
                              + (f": {decision.notes}" if decision.notes else ""))
            if decision.verdict == "reject" and gate.call is None:
                raise Aborted(f"gate {gate.name} is approve/abort only — nothing can "
                              f"revise it; rejected by {decision.by}: {decision.notes}")
            limit = run.cfg.hitl.max_rounds
            if decision.verdict == "reject" and limit and round >= limit:
                raise Aborted(f"gate {gate.name} rejected {round} time(s) — "
                              f"hitl.max_rounds reached")
        if decision.approved:
            if decision.notes:
                envelope.notes_for_next_agent = (
                    f"{envelope.notes_for_next_agent}\n\n"
                    f"Engineer's remarks at the {gate.name} gate: {decision.notes}").strip()
            return envelope

        with run.phase(PhaseParams(name=f"{gate.name}_revise_{round}", kind="agent",
                                   owner=gate.owner, retries=gate.retries,
                                   description=f"Rework the {gate.name} along the "
                                               f"engineer's notes, in the same session")) as ph:
            envelope = ph.call(gate.call.model_copy(update={
                "previous": decision,
                "prompt": REVISE_PROMPT.format(prompt=gate.call.prompt)}))
        round += 1


def _already_approved(run, gate: Gate, envelope: EnvelopeBase) -> bool:
    recorded = read_decision(run.session_dir, gate.name, 1)
    if recorded is None or not recorded.approved:
        return False
    return recorded.subject_digest == digest(resolve_paths(run, gate.paths or envelope.artifacts))


def _pass_by_policy(run, gate: Gate, envelope: EnvelopeBase) -> EnvelopeBase:
    """Trust, written down: the gate was passed, and the record says by whom."""
    paths = resolve_paths(run, gate.paths or envelope.artifacts)
    stamp = now_iso()
    decision = Decision(gate=gate.name, round=1, verdict="approve", by="policy",
                        channel="auto", subject_digest=digest(paths), decided_at=stamp,
                        consumed_at=stamp)
    record(run.session_dir, decision)
    run.tracer.event(EventRecord(adw_id=run.adw_id,
                                 phase_id=run.phases[-1].phase_id if run.phases else "",
                                 type="decision", name=gate.name,
                                 payload=decision.model_dump()))
    run.console.note(f"gate {gate.name}: passed by policy (hitl is off for it)")
    return envelope
