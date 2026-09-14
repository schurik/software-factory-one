"""The runtime directory IS the record. This module reads and writes it.

`asf/data/sessions/<adw_id>/` already holds everything a run produced —
`events.jsonl`, each agent's `envelope.json`, its compiled `prompts/`, the raw
harness stream, `agent_map.json`, `context_handoff/`. The trace db is the
queryable MIRROR of the same events, kept for the visualizer to poll; nothing
the factory does at runtime depends on it. So anything that needs to know what
a session has already done reads these files, never sqlite: a run works when
the db is missing, when it has been deleted to reclaim disk, or when the
visualizer is holding it — and the record travels with the session directory.

Three artifacts are written here that the rest of the record could not supply:

  * `run.json` — the session's own state: which workflow ran, the ARGV that
    started it, the pid, and how it ended. `events.jsonl` describes phases, not
    the process that opened them, and `just resume` has to know what to launch
    again.
  * `envelopes/<phase_id>.json` — one file per agent PHASE. The per-agent
    `envelope.json` beside it is last-wins, which is right for "what did the
    builder last say" and useless for a replay: a builder that built, fixed and
    revised leaves one file and three phases.
  * `processes.jsonl` — every process this session spawned and every one that
    ended, appended as it happens. A hung coding agent emits nothing at all,
    which is exactly when you need its pid, and `ps` cannot say which run a pid
    belongs to. `just kill` reads this.

Both are small, both are rewritten rather than appended, and neither is read by
anything but the factory itself.

The same rule covers the two watchers, whose liveness is not a session at all:
`watchers/<kind>.json` is what `just status` reads. The heartbeat is still
written to the db as well, because the trace UI renders those badges — a WRITE
is fine, and the db is where the visualizer looks.

**Nothing here reads sqlite, and nothing in the factory should.** The db is the
event log the visualizer polls; the day the events go to a hosted API instead,
there is no db on this machine to query and every answer below still works,
because every answer below is a file this session wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

from .data_types import RecordedPhase, RunState, WaitingFor
from .utils import ensure_dir

RUN_FILE = "run.json"
ENVELOPES_DIR = "envelopes"
EVENTS_FILE = "events.jsonl"


# ── run.json ─────────────────────────────────────────────────────────────────

def run_path(session_dir: Path) -> Path:
    return Path(session_dir) / RUN_FILE


def read_run(session_dir: Path) -> RunState | None:
    """The session's recorded state, or None when it has none yet."""
    path = run_path(session_dir)
    if not path.is_file():
        return None
    try:
        return RunState(**json.loads(path.read_text()))
    except (ValueError, OSError):
        return None            # a truncated write is a missing answer, not a crash


def write_run(session_dir: Path, state: RunState) -> None:
    ensure_dir(Path(session_dir))
    run_path(session_dir).write_text(state.model_dump_json(indent=2))


def start_run(session_dir: Path, state: RunState) -> RunState:
    """Record that a process has taken this session, keeping what came before.

    A joined session is worked by more than one ADW, and `workflows` is the list
    of them in order — the same story `sessions.adw_name` tells in the db. The
    command and the pid are the NEWEST process's, because they answer "what
    would running this again mean", and the newest process is the one that was
    working when the session stopped.
    """
    previous = read_run(session_dir)
    if previous:
        state.workflows = previous.workflows + [
            name for name in state.workflows if name not in previous.workflows]
        # Provenance is learned once and never unlearned — an issue-triggered
        # session that a later ADW re-enters is still issue-triggered.
        state.trigger = state.trigger or previous.trigger
        state.issue_url = state.issue_url or previous.issue_url
        state.issue_number = state.issue_number or previous.issue_number
        state.issue_project = state.issue_project or previous.issue_project
        state.pr_url = state.pr_url or previous.pr_url
        # A session stopped at a gate is picked up by the process that answers
        # it, and that process has to reach the gate knowing what was asked.
        state.waiting_for = state.waiting_for or previous.waiting_for
        # Spend is the session's, not the process's, and a new process starts
        # its record at zero — so carrying it is what keeps `budget:` a ceiling
        # on the work rather than on whoever happens to be running it. Dropping
        # these two lines hands every re-entry a fresh wallet.
        state.total_tokens = previous.total_tokens
        state.total_cost = previous.total_cost
    write_run(session_dir, state)
    return state


def finish_run(session_dir: Path, status: str) -> None:
    """Close the session's record. Never raises: a run must not die reporting.

    Called from `run.finish()`, from a failed phase, and from the SIGTERM
    handler, so the file agrees with the db about how the session ended even
    when the ending was not the happy one.
    """
    from .utils import now_iso
    state = read_run(session_dir)
    if state is None:
        return
    state.status = status
    state.ended_at = now_iso()
    try:
        write_run(session_dir, state)
    except OSError:
        pass
    end_all_processes(session_dir)   # nothing this run started is still its to stop


def update_run(session_dir: Path, **fields) -> None:
    """Patch the recorded state in place (provenance, a pull request url)."""
    state = read_run(session_dir)
    if state is None:
        return
    for key, value in fields.items():
        if value:
            setattr(state, key, value)
    try:
        write_run(session_dir, state)
    except OSError:
        pass


# ── run.json: waiting on a human ─────────────────────────────────────────────

DECISIONS_DIR = "decisions"


def decisions_dir(session_dir: Path) -> Path:
    return Path(session_dir) / DECISIONS_DIR


def suspend_run(session_dir: Path, waiting: WaitingFor) -> None:
    """Record that this session stopped for a human, and that nothing of it is alive.

    Not `finish_run`: `ended_at` stays empty, because a waiting run has not
    ended — it will be picked up by `just approve` and continue as the same
    session. The pid is cleared and the process rows closed for the same reason
    `finish_run` closes them: the process IS gone, and a watcher counting live
    runs must not count this one.
    """
    state = read_run(session_dir)
    if state is None:
        return
    state.status = "waiting"
    state.waiting_for = waiting
    state.pid = 0
    try:
        write_run(session_dir, state)
    except OSError:
        pass
    end_all_processes(session_dir)


def clear_waiting(session_dir: Path) -> None:
    """The decision was consumed; the session no longer waits on it."""
    state = read_run(session_dir)
    if state is None or state.waiting_for is None:
        return
    state.waiting_for = None
    try:
        write_run(session_dir, state)
    except OSError:
        pass


def waiting_sessions(sessions_dir: Path) -> dict[str, WaitingFor]:
    """{adw_id: what it waits for} for every session stopped at a gate."""
    return {adw_id: state.waiting_for for adw_id, state in scan(sessions_dir).items()
            if state.status == "waiting" and state.waiting_for is not None}


# ── envelopes/<phase_id>.json ────────────────────────────────────────────────

def write_envelope(session_dir: Path, record: RecordedPhase) -> None:
    """One agent phase's envelope, keyed by the phase that produced it."""
    directory = ensure_dir(Path(session_dir) / ENVELOPES_DIR)
    (directory / f"{record.phase_id}.json").write_text(record.model_dump_json(indent=2))


def recorded_phases(session_dir: Path) -> list[RecordedPhase]:
    """Every agent phase this session completed successfully, in run order.

    Two session artifacts answer this together, and both are needed. The
    envelope files say what each agent PRODUCED; `events.jsonl` says which
    phases actually PASSED — an envelope is written before the phase closes, so
    one whose phase then failed (a bad status, a permission breach) is on disk
    and must never be handed back. A phase with no `phase_end` recorded (the
    process was killed mid-phase) is unfinished, and therefore not offered.
    """
    directory = Path(session_dir) / ENVELOPES_DIR
    if not directory.is_dir():
        return []
    passed = _phase_outcomes(Path(session_dir))
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = RecordedPhase(**json.loads(path.read_text()))
        except (ValueError, OSError):
            continue           # an unreadable record is one that is not offered
        if passed.get(record.phase_id) == "success":
            records.append(record)
    return sorted(records, key=lambda record: record.seq)


def max_phase_seq(session_dir: Path, adw_id: str) -> int:
    """The highest phase number this session has already used; 0 when it is new.

    A joined or resumed run continues the sequence instead of restarting at 1 —
    restarting collides with the first process's phases on both the ordering and
    the `phase_id`, which is the name of the envelope file this module writes.
    Read from `events.jsonl` rather than the db for the reason the rest of this
    module exists: a run whose db was deleted must still number its phases
    correctly, and the seq is right there in every phase id the session emitted
    (`<adw_id>_<seq>_<name>`, so the prefix comes off and the digits are next).
    """
    highest = 0
    for phase_id in _phase_outcomes(session_dir, every=True):
        tail = phase_id.removeprefix(f"{adw_id}_").split("_", 1)[0]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return highest


def _phase_outcomes(session_dir: Path, every: bool = False) -> dict[str, str]:
    """{phase_id: final status} from the session's own event log.

    `events.jsonl` is the raw record the tracer appends as things happen — the
    same lines the db mirrors. Read forwards, so a phase re-entered by a later
    process in the session ends on its LATEST outcome. `every` widens it to
    phases that only ever STARTED, which is what counting the phase numbers a
    session has used needs — see `max_phase_seq`.
    """
    path = session_dir / EVENTS_FILE
    if not path.is_file():
        return {}
    outcomes: dict[str, str] = {}
    try:
        with path.open() as stream:
            for line in stream:
                line = line.strip()
                wanted = ('"phase_' if every else '"phase_end"')
                if not line or wanted not in line:
                    continue   # cheap reject: most lines are logs and tool calls
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not event.get("phase_id"):
                    continue
                if event.get("type") == "phase_end":
                    outcomes[event["phase_id"]] = (event.get("payload") or {}).get("status", "")
                elif every and event.get("type") == "phase_start":
                    outcomes.setdefault(event["phase_id"], "")
    except OSError:
        return {}
    return outcomes


# ── asking about many sessions at once ───────────────────────────────────────

def sessions_root(main_root, data_dir: str) -> Path:
    """Where session directories live, from the two things every caller has."""
    from .utils import anchor
    return anchor(main_root, f"{data_dir}/sessions")


def scan(sessions_dir: Path) -> dict[str, RunState]:
    """{adw_id: RunState} for every session on disk. {} when there are none.

    What the four `SELECT ... FROM sessions` readers in `tracer.py` used to do,
    against the files instead: the maintenance tools — the watchers deciding how
    many runs are in flight, `just worktrees` labelling a directory, `just
    status` — ask about sessions they did not run, and a db is not required to
    answer. A session directory with no `run.json` (recorded before this file
    existed) is absent from the result, and every caller already renders that as
    "unknown" rather than guessing.
    """
    directory = Path(sessions_dir)
    if not directory.is_dir():
        return {}
    found: dict[str, RunState] = {}
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        state = read_run(child)
        if state:
            found[state.adw_id or child.name] = state
    return found


def statuses(sessions_dir: Path) -> dict[str, str]:
    """{adw_id: status} — 'running' means the record was never closed."""
    return {adw_id: state.status or "unknown"
            for adw_id, state in scan(sessions_dir).items()}


def running_pids(sessions_dir: Path) -> dict[str, int]:
    """{adw_id: pid} for every session that BELIEVES it is running.

    "Believes" is the point, and the caller is expected to check the pid. A
    record is closed by `run.finish()` or by the SIGTERM handler; a SIGKILL, an
    OOM or a reboot leaves it reading `running` forever, and anything that
    budgets on the count (the issue watcher's `max_concurrent`) would wedge
    permanently after two such deaths without the pid to test.
    """
    return {adw_id: state.pid for adw_id, state in scan(sessions_dir).items()
            if state.status == "running" and state.pid}


def pr_urls(sessions_dir: Path) -> dict[str, str]:
    """{adw_id: pr_url} for every session that became a pull request."""
    return {adw_id: state.pr_url for adw_id, state in scan(sessions_dir).items()
            if state.pr_url}


def adw_names(sessions_dir: Path) -> dict[str, str]:
    """{adw_id: "issue + pr-review"} — which workflows a session ran.

    `pr_watch` reaps with this: stopping a review run whose branch has landed is
    cleanup, and stopping the SDLC run that opened that pull request and is
    still writing its docs is destroying work. Only the workflow tells them apart.
    """
    return {adw_id: state.adw_name for adw_id, state in scan(sessions_dir).items()}


# ── watchers/<kind>.json (liveness, not a session) ───────────────────────────

def watchers_dir(main_root, data_dir: str) -> Path:
    from .utils import anchor
    return anchor(main_root, f"{data_dir}/watchers")


def watcher_beat(directory: Path, kind: str, fields: dict) -> None:
    """Record that a watcher is alive and what it last saw. Never raises.

    A watcher that is not running and a watcher with nothing to do look
    identical from the outside — which is the most expensive confusion in
    operating this thing: you label an issue, wait, and find out an hour later
    that nothing was polling. This file is what lets `just status` answer it.

    `started_at` is preserved across beats, so the file also says how long this
    watcher has been up. Failure is swallowed: a watcher must not die because it
    could not describe itself.
    """
    try:
        path = ensure_dir(Path(directory)) / f"{kind}.json"
        previous = {}
        if path.is_file():
            try:
                previous = json.loads(path.read_text())
            except ValueError:
                previous = {}
        row = {**previous, **fields, "kind": kind}
        row["started_at"] = previous.get("started_at") or fields.get("started_at", "")
        path.write_text(json.dumps(row, indent=2))
    except OSError:
        pass


def watcher_states(directory: Path) -> dict[str, dict]:
    """{kind: row} for every watcher that has ever beaten here.

    A kind that is absent has never been started in this repo — a different
    thing from `stopped`, and readers render the two differently.
    """
    path = Path(directory)
    if not path.is_dir():
        return {}
    found = {}
    for child in sorted(path.glob("*.json")):
        try:
            row = json.loads(child.read_text())
        except (ValueError, OSError):
            continue
        found[row.get("kind") or child.stem] = row
    return found


# ── processes.jsonl (what a run has alive, so it can be stopped) ─────────────

PROCESSES_FILE = "processes.jsonl"


def record_process(session_dir: Path, kind: str, name: str, pid: int,
                   command: str) -> None:
    """Append a process this session just spawned. Never raises.

    Appended rather than rewritten because two agents can be starting and
    ending at once, and an append of one line is the only write that needs no
    coordination between them.
    """
    from .utils import now_iso
    try:
        path = ensure_dir(Path(session_dir)) / PROCESSES_FILE
        with path.open("a") as stream:
            stream.write(json.dumps({"event": "start", "kind": kind, "name": name,
                                     "pid": pid, "command": command[:500],
                                     "at": now_iso()}) + "\n")
    except OSError:
        pass


def end_process(session_dir: Path, pid: int) -> None:
    """Append the fact that a pid this session started is finished."""
    from .utils import now_iso
    try:
        path = ensure_dir(Path(session_dir)) / PROCESSES_FILE
        with path.open("a") as stream:
            stream.write(json.dumps({"event": "end", "pid": pid,
                                     "at": now_iso()}) + "\n")
    except OSError:
        pass


def live_processes(session_dir: Path) -> list[dict]:
    """What this session believes it still has running — children first.

    "Believes" again: a SIGKILL leaves a start with no end. The caller verifies
    each pid against the command recorded beside it, because pids get recycled
    and signalling a stranger's process is the one outcome worth checking for.

    Children before the parent, deliberately. Kill the workflow first and its
    coding agent keeps running, detached, still burning tokens against an API
    with nothing left to record what it did.
    """
    path = Path(session_dir) / PROCESSES_FILE
    if not path.is_file():
        return []
    live: dict[int, dict] = {}
    try:
        with path.open() as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                pid = row.get("pid")
                if not pid:
                    continue
                if row.get("event") == "start":
                    live[pid] = row
                else:
                    live.pop(pid, None)
    except OSError:
        return []
    return sorted(live.values(), key=lambda row: 0 if row.get("kind") == "agent" else 1)


def end_all_processes(session_dir: Path) -> None:
    """Close every process this session still believes is alive.

    Called when the session ends: the run is over, so nothing it started is
    still its to stop.
    """
    for row in live_processes(session_dir):
        end_process(session_dir, row["pid"])
