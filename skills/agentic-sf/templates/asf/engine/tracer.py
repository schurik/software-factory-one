"""Tracer: every event lands in JSONL and SQLite AS IT HAPPENS.

Files are the raw record; asf.db is the queryable mirror the UI polls.
No push transport — the flow is always: agents -> sqlite -> web ui.
WAL mode so the UI can read while ADW processes write.

THE FACTORY ONLY WRITES HERE. Nothing in it reads this db back — not a run,
not a watcher, not a maintenance command. Every question about what a session
did is answered from that session's own directory, through
`engine/artifacts.py`.

That is not tidiness, it is portability: the db is a local mirror of the event
stream, and the day those events go to a hosted API instead there is no file on
this machine to query. Code that reads it would have to be written twice. Code
that reads the session directory does not, because the session directory is
where the run actually happened.

`watcher_beat` is the one row here that is not a run's: it is written for the
trace UI's badges, and `just status` reads the same heartbeat from
`adw_data/watchers/<kind>.json` rather than from this file.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .data_types import AgentConfig, EventRecord, GateReport, Phase, Workspace
from .utils import ensure_dir, new_id, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  adw_id        TEXT PRIMARY KEY,
  adw_name      TEXT,                -- workflow(s) run, e.g. "ship" or "issue + pr-review"
  request       TEXT,
  status        TEXT,
  engineer      TEXT,
  started_at    TEXT, ended_at TEXT,
  total_tokens  INTEGER DEFAULT 0, total_cost REAL DEFAULT 0,
  archived      INTEGER DEFAULT 0,  -- review triage, set by the UI; never by a run
  -- Where the run actually executed. A worktree is pruned when the run is
  -- accepted, so the tree it worked in has to be recorded while it still
  -- exists — and the branch outlives both, which makes these four the answer
  -- to "where did this run's work go".
  repo_root     TEXT,
  branch        TEXT,
  base_ref      TEXT,
  base_commit   TEXT,
  -- What asked for this run. `trigger` is how it started at all ('engineer' |
  -- 'issue'); `issue_url` is canonical rather than a number, because a number
  -- means nothing without the project it belongs to.
  trigger       TEXT,
  issue_url     TEXT,
  -- Where the run's branch ended up, recorded by the integration phase. The
  -- URL is the pointer; WHY a branch did not land stays in that phase's notes,
  -- because a refusal is a paragraph and this is a column.
  pr_url        TEXT
);
CREATE TABLE IF NOT EXISTS phases (
  phase_id      TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  seq           INTEGER,
  name TEXT, kind TEXT, owner TEXT, description TEXT,
  status        TEXT DEFAULT 'fail',
  attempt       INTEGER DEFAULT 0, retries INTEGER DEFAULT 0,
  error         TEXT,
  started_at    TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id      TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  parent_id     TEXT,
  type          TEXT,
  name          TEXT,
  payload_json  TEXT,
  tokens        INTEGER,
  started_at    TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS envelopes (
  envelope_id   TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  agent         TEXT,
  output_type   TEXT,
  payload_json  TEXT,
  valid         INTEGER,
  attempt       INTEGER,
  created_at    TEXT
);
CREATE TABLE IF NOT EXISTS gate_results (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  attempt       INTEGER,
  gate          TEXT,
  passed        INTEGER,
  violations_json TEXT,
  checks_json   TEXT,               -- [{item, ok, note}] — WHAT the gate verified
  created_at    TEXT
);
CREATE TABLE IF NOT EXISTS processes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  adw_id        TEXT REFERENCES sessions,
  kind          TEXT,                -- 'adw' (the workflow process) | 'agent' (a coding-agent child)
  name          TEXT,                -- '' for the adw, the agent name for a child
  pid           INTEGER,
  command       TEXT,                -- what the pid was, so a recycled pid is not killed by mistake
  started_at    TEXT, ended_at TEXT  -- ended_at NULL = believed alive
);
CREATE TABLE IF NOT EXISTS watchers (
  -- One row per watcher KIND, not per process: two issue watchers on one repo
  -- is the thing to notice, not to record twice, and the pid says which one
  -- wrote the row last. Rewritten on every poll; nothing here is history.
  kind          TEXT PRIMARY KEY,    -- 'issues' | 'prs'
  status        TEXT,                -- 'polling' | 'working' | 'stopped' | 'disabled' | 'error'
  pid           INTEGER,             -- what to probe for liveness; only meaningful on this machine
  project       TEXT,
  interval_s    INTEGER,
  note          TEXT,                -- one line on what the last poll saw
  started_at    TEXT, last_poll_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_sessions (
  adw_id        TEXT REFERENCES sessions,
  agent         TEXT,
  harness       TEXT, model TEXT, color TEXT,
  session_id    TEXT,
  context_tokens INTEGER,           -- window occupancy after the agent's last turn
  context_window INTEGER,           -- the model's ceiling; 0/NULL = unknown
  created_at    TEXT, last_used_at TEXT,
  PRIMARY KEY (adw_id, agent)
);
"""

# Columns added after a schema shipped. CREATE TABLE IF NOT EXISTS never
# revisits an existing table, so additive changes need an explicit ALTER.
MIGRATIONS = [("agent_sessions", "color", "TEXT"),
              ("gate_results", "checks_json", "TEXT"),
              ("sessions", "adw_name", "TEXT"),
              ("agent_sessions", "context_tokens", "INTEGER"),
              ("agent_sessions", "context_window", "INTEGER"),
              ("sessions", "archived", "INTEGER DEFAULT 0"),
              ("sessions", "repo_root", "TEXT"),
              ("sessions", "branch", "TEXT"),
              ("sessions", "base_ref", "TEXT"),
              ("sessions", "base_commit", "TEXT"),
              ("sessions", "trigger", "TEXT"),
              ("sessions", "issue_url", "TEXT"),
              ("sessions", "pr_url", "TEXT")]


def ensure_db(db_path: str | Path) -> sqlite3.Connection:
    """Open the trace db, creating it with the FULL schema if it is missing.

    Not a reader, and not a Tracer either: the two writers that are not runs —
    `watcher_beat` here, and `up.py` making sure the visualizer has something to
    open on a repo that has never run anything — both need a db to exist without
    minting a session or an events file to go with it.

    The full schema rather than the one table the caller cares about, because a
    db holding only `watchers` fails every session query in the UI, which is a
    worse outcome than the missing file it replaced. The caller closes it.
    """
    path = Path(db_path)
    ensure_dir(path.parent)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(SCHEMA)
    return conn


def watcher_beat(db_path: str | Path, kind: str, status: str, *,
                 pid: int = 0, project: str = "", interval_s: int = 0,
                 note: str = "") -> None:
    """Record that a watcher is alive and what it last saw. Never raises.

    The only write in this module that does not belong to a run, and it is for
    the TRACE UI: the badges in its top bar read this table. `just status` reads
    the same heartbeat from `adw_data/watchers/<kind>.json`, which
    `artifacts.watcher_beat` writes on the same poll — so the answer survives a
    machine with no db, and the badge keeps working on one that has it.

    Either way it exists because a watcher that is not running and a watcher
    with nothing to do look identical from the outside — the single most
    expensive confusion in operating this thing: you label an issue, wait, and
    find out an hour later that nothing was polling.

    Written on every poll, so `last_poll_at` doubles as the liveness signal for
    a watcher that is idling. It is NOT the signal for one that is mid-run:
    `_launch` blocks for as long as the ADW takes, and status `working` is what
    says so. The pid is how a reader tells a working watcher from a killed one.

    `started_at` is preserved across beats — an upsert, not a replace — so the
    row also says how long this watcher has been up.

    Failure is swallowed on purpose. A watcher must not die because it could
    not describe itself, and the db may legitimately be missing on the first
    beat of a repo that has never run anything.
    """
    try:
        conn = ensure_db(db_path)
        try:
            ts = now_iso()
            conn.execute(
                "INSERT INTO watchers (kind, status, pid, project, interval_s, note,"
                " started_at, last_poll_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(kind) DO UPDATE SET status=excluded.status,"
                " pid=excluded.pid, project=excluded.project,"
                " interval_s=excluded.interval_s, note=excluded.note,"
                " last_poll_at=excluded.last_poll_at",
                (kind, status, pid, project, interval_s, note, ts, ts))
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        pass


class Tracer:
    def __init__(self, db_path: str | Path, events_jsonl: str | Path):
        ensure_dir(Path(db_path).parent)
        self.db_path = str(db_path)
        self.events_jsonl = Path(events_jsonl)
        ensure_dir(self.events_jsonl.parent)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Additive column migrations, so a db from an older factory still opens."""
        for table, column, decl in MIGRATIONS:
            columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # ── events ──────────────────────────────────────────────────────────────
    def event(self, record: EventRecord) -> str:
        event_id = f"evt_{new_id(12)}"
        ts = now_iso()
        line = {"event_id": event_id, "ts": ts, **record.model_dump()}
        with self.events_jsonl.open("a") as f:
            f.write(json.dumps(line) + "\n")
        self.conn.execute(
            "INSERT INTO events (event_id, adw_id, phase_id, parent_id, type, name,"
            " payload_json, tokens, started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, record.adw_id, record.phase_id, record.parent_id, record.type,
             record.name, json.dumps(record.payload), record.tokens,
             record.started_at or ts, record.ended_at),
        )
        return event_id

    # ── sessions ────────────────────────────────────────────────────────────
    def session_start(self, adw_id: str, engineer: str, adw_name: str | None = None) -> None:
        # `trigger` is stamped 'engineer' here rather than left null, so null
        # means one thing only: a row written before the column existed. A run
        # that nobody can classify and a run somebody typed are different
        # answers, and an analytics query that cannot tell them apart will
        # quietly report the first as the second. An issue phase overwrites it.
        self.conn.execute(
            "INSERT INTO sessions (adw_id, status, engineer, started_at, trigger) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(adw_id) DO UPDATE SET status='running'",
            (adw_id, "running", engineer, now_iso(), "engineer"),
        )
        if not adw_name:
            return
        # A joined session chains ADWs — record each distinct one, in run order.
        row = self.conn.execute("SELECT adw_name FROM sessions WHERE adw_id=?",
                                (adw_id,)).fetchone()
        names = row[0].split(" + ") if row and row[0] else []
        if adw_name not in names:
            names.append(adw_name)
            self.conn.execute("UPDATE sessions SET adw_name=? WHERE adw_id=?",
                              (" + ".join(names), adw_id))

    def session_workspace(self, adw_id: str, workspace: Workspace) -> None:
        """Record where the run executes, before any phase opens.

        Written at session start rather than at the end, because a killed run is
        exactly the one whose worktree you need to find, and it never reaches an
        end.
        """
        self.conn.execute(
            "UPDATE sessions SET repo_root=?, branch=?, base_ref=?, base_commit=?"
            " WHERE adw_id=?",
            (str(workspace.repo_root), workspace.branch, workspace.base_ref,
             workspace.base_commit, adw_id),
        )

    def session_request(self, adw_id: str, request: str) -> None:
        self.conn.execute("UPDATE sessions SET request=? WHERE adw_id=?",
                          (request[:500], adw_id))

    def session_issue(self, adw_id: str, url: str, trigger: str = "issue") -> None:
        """Tie this run to the work item that caused it. One half of the pair.

        The other half is the comment the run posts back on the issue. Both are
        needed: this answers "which run belongs to #42" from the trace, and the
        comment answers "which issue produced this branch" from the tracker.
        """
        self.conn.execute("UPDATE sessions SET issue_url=?, trigger=? WHERE adw_id=?",
                          (url[:500], trigger, adw_id))

    def session_trigger(self, adw_id: str, trigger: str) -> None:
        """Record WHAT asked for this run, without touching where it came from.

        `session_issue()` cannot do this job: it writes `issue_url` in the same
        statement, so using it to mark a run as review-triggered would blank the
        issue that started the session — and `integration.integrate()` reads
        both. A pull request under review has no issue url to offer, and a
        session that had one must keep it.
        """
        if not trigger:
            return
        self.conn.execute("UPDATE sessions SET trigger=? WHERE adw_id=?",
                          (trigger, adw_id))

    def session_pr(self, adw_id: str, url: str) -> None:
        """Record the pull request this run's branch became.

        Written from integration.integrate() rather than from an ADW, so no
        chain can land a branch and forget to say where it went. Empty urls are
        ignored: a merge and a refusal both produce none, and overwriting a real
        url with "" on a second integration attempt would lose the pointer.
        """
        if not url:
            return
        self.conn.execute("UPDATE sessions SET pr_url=? WHERE adw_id=?",
                          (url[:500], adw_id))

    def session_finish(self, adw_id: str, ok: bool) -> None:
        self.conn.execute(
            "UPDATE sessions SET status=?, ended_at=? WHERE adw_id=?",
            ("success" if ok else "fail", now_iso(), adw_id),
        )
        self.processes_end_all(adw_id)   # nothing of this run is alive any more

    def session_waiting(self, adw_id: str, gate: str) -> None:
        """The run stopped at a gate. Its process is gone; its session is not over.

        `ended_at` stays NULL for the reason `artifacts.suspend_run` leaves it
        empty: the session continues when a human answers. The next process's
        `session_start` flips the row back to `running`.
        """
        self.conn.execute("UPDATE sessions SET status='waiting' WHERE adw_id=?", (adw_id,))
        self.processes_end_all(adw_id)

    def session_add_usage(self, adw_id: str, tokens: int, cost: float) -> None:
        """Spend, for the UI's card. The BUDGET reads `run.json`, not this row:
        a ceiling that only worked where the db exists would be a ceiling this
        factory cannot promise. See runner.add_usage, which writes both."""
        self.conn.execute(
            "UPDATE sessions SET total_tokens=total_tokens+?, total_cost=total_cost+? WHERE adw_id=?",
            (tokens, cost, adw_id),
        )

    # ── processes (adw_id → pid, so a hung run can be found and killed) ─────
    def process_start(self, adw_id: str, kind: str, name: str, pid: int,
                      command: str) -> None:
        """Record a live process for this run.

        A coding agent that hangs produces no events at all, which is exactly
        when you need its pid — and `ps` cannot tell you which adw_id it
        belongs to. Writing it here makes the trace the answer to "what is this
        run running, and how do I stop it".
        """
        self.conn.execute(
            "INSERT INTO processes (adw_id, kind, name, pid, command, started_at)"
            " VALUES (?,?,?,?,?,?)",
            (adw_id, kind, name, pid, command[:500], now_iso()),
        )

    def process_end(self, adw_id: str, pid: int) -> None:
        """Mark the newest live row for this pid as finished."""
        self.conn.execute(
            "UPDATE processes SET ended_at=? WHERE id = ("
            "  SELECT id FROM processes WHERE adw_id=? AND pid=? AND ended_at IS NULL"
            "  ORDER BY id DESC LIMIT 1)",
            (now_iso(), adw_id, pid),
        )

    def processes_end_all(self, adw_id: str) -> None:
        """Close out every live row for a run — called when the session ends."""
        self.conn.execute(
            "UPDATE processes SET ended_at=? WHERE adw_id=? AND ended_at IS NULL",
            (now_iso(), adw_id),
        )

    # ── phases ──────────────────────────────────────────────────────────────
    def phase_upsert(self, phase: Phase) -> None:
        p = phase.params
        self.conn.execute(
            "INSERT INTO phases (phase_id, adw_id, seq, name, kind, owner, description,"
            " status, attempt, retries, error, started_at, ended_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(phase_id) DO UPDATE SET status=excluded.status,"
            " attempt=excluded.attempt, error=excluded.error, ended_at=excluded.ended_at",
            (phase.phase_id, phase.adw_id, phase.seq, p.name, p.kind, p.owner,
             p.description, phase.status, phase.attempt, p.retries, phase.error,
             phase.started_at, phase.ended_at),
        )

    # ── envelopes / gates / agent sessions ──────────────────────────────────
    def envelope_row(self, phase: Phase, agent: str, output_type: str,
                     payload_json: str, valid: bool, attempt: int) -> None:
        self.conn.execute(
            "INSERT INTO envelopes (envelope_id, adw_id, phase_id, agent, output_type,"
            " payload_json, valid, attempt, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"env_{new_id(12)}", phase.adw_id, phase.phase_id, agent, output_type,
             payload_json, int(valid), attempt, now_iso()),
        )

    def gate_row(self, phase: Phase, gate: str, report: GateReport, attempt: int) -> None:
        """The report carries both the verdict and the evidence behind it."""
        self.conn.execute(
            "INSERT INTO gate_results (adw_id, phase_id, attempt, gate, passed,"
            " violations_json, checks_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (phase.adw_id, phase.phase_id, attempt, gate, int(report.passed),
             json.dumps(report.violations),
             json.dumps([c.model_dump() for c in report.checks]), now_iso()),
        )

    def agent_session_row(self, adw_id: str, agent: AgentConfig, session_id: str,
                          context_tokens: int = 0, context_window: int = 0) -> None:
        """The agent's config row is the source of truth for its label and color.

        Context is carried here rather than derived from events because the lane
        wants one number per agent — the latest — and a session that runs the
        same agent twice overwrites it, exactly like model and session_id.
        """
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO agent_sessions (adw_id, agent, harness, model, color,"
            " session_id, context_tokens, context_window, created_at, last_used_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(adw_id, agent) DO UPDATE SET model=excluded.model,"
            " color=excluded.color, session_id=excluded.session_id,"
            " context_tokens=excluded.context_tokens,"
            " context_window=excluded.context_window,"
            " last_used_at=excluded.last_used_at",
            (adw_id, agent.name, agent.harness, agent.model, agent.color,
             session_id, context_tokens, context_window, ts, ts),
        )
