"""`asf up` — the trace UI and both watchers in one foreground process; `asf status`.

The factory's long-running parts are the trace UI, the issue watcher and the
review watcher, and three commands in three terminals go wrong every week:
you forget one, and a watcher that is not running looks exactly like a
watcher with nothing to do. So this is a supervisor: one process that owns
every child, prefixes their output, restarts what dies, and takes the whole
tree down with it. NOT A DAEMON — the terminal it runs in is the handle.

The trace UI ships with the skill (`apps/visualizer`), reached through the
`ASF_SKILL` the installer wrote into `.env` (`preflight.visualizer_dir()`, so
`doctor` answers from the same place). Absent, `up` runs without it and says
so as a WARNING — a service that silently did not start looks exactly like a
service with nothing to say; the watchers are the part that must not be
forgotten.

`status` answers the other half: is anything running right now, and did it
poll recently — from the watcher heartbeat FILES and a probe of each pid, so a
watcher killed with SIGKILL reads as gone rather than as its last row.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import artifacts, git_helper, preflight, worktree
from .data_types import FactoryConfig
from .utils import anchor

RUNNER = "asf/asf.py"
API_PORT = int(os.environ.get("PORT", "4600"))
UI_PORT = 4601
COLORS = {"obs": "\033[36m", "ui": "\033[35m", "issues": "\033[33m", "prs": "\033[32m"}
DIM, WARN, RESET = "\033[2m", "\033[33m", "\033[0m"


def paint(color: str, text: str) -> str:
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else text


@dataclass
class Service:
    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    proc: subprocess.Popen | None = None
    restarts: int = 0
    started_at: float = 0.0
    give_up: bool = False


def _spawn(service: Service, on_line) -> None:
    """Its OWN process group, so `bun run vite` and the runner's children can
    be signalled whole — an orphan holding :4600 is what this exists to stop."""
    service.proc = subprocess.Popen(
        service.argv, cwd=str(service.cwd), env={**os.environ, **service.env},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        start_new_session=True)
    service.started_at = time.monotonic()
    threading.Thread(target=_pump, args=(service, on_line), daemon=True).start()


def _pump(service: Service, on_line) -> None:
    assert service.proc and service.proc.stdout
    for line in service.proc.stdout:
        on_line(service.name, line.rstrip("\n"))


def _stop(service: Service, grace: float = 8.0) -> None:
    """SIGTERM the group (the watchers turn it into a `stopped` beat), then SIGKILL."""
    proc = service.proc
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def wanted(cfg: FactoryConfig, only: str) -> set[str]:
    if only:
        chosen = {part.strip() for part in only.split(",") if part.strip()}
        unknown = chosen - {"obs", "issues", "prs"}
        if unknown:
            raise SystemExit(f"--only: unknown service(s) {', '.join(sorted(unknown))} — "
                             f"pick from obs, issues, prs")
        return chosen
    want = {"obs", "issues", "prs"}
    if not cfg.issues.enabled:
        print(paint(DIM, "  ~ issues.enabled is false — not starting the issue watcher"))
        want.discard("issues")
    if not cfg.pull_requests.enabled:
        print(paint(DIM, "  ~ pull_requests.enabled is false — not starting the review watcher"))
        want.discard("prs")
    return want


def check(cfg: FactoryConfig, want: set[str]) -> list[str]:
    """What would stop THESE services. Non-fatal findings drop the service."""
    if "obs" in want:
        home = preflight.visualizer_dir()
        if home is None:
            print(paint(WARN, "  ! no trace UI: ASF_SKILL in .env must point at the skill "
                              "directory (the visualizer ships there) — install.py writes "
                              "it, and a clone without its .env has none"))
            want.discard("obs")
        elif not shutil.which("bun"):
            print(paint(WARN, "  ! bun is not on PATH — starting without the trace UI"))
            want.discard("obs")
        elif not preflight.port_free(API_PORT):
            print(paint(WARN, f"  ! something already listens on :{API_PORT} — starting "
                              f"without the trace UI: `lsof -ti :{API_PORT} | xargs kill`"))
            want.discard("obs")
    forge = (cfg.issues.list_command or ["gh"])[0]
    if want & {"issues", "prs"} and not shutil.which(forge):
        print(paint(WARN, f"  ! {forge!r} is not on PATH — the watchers can start, but every "
                          f"poll will fail to list anything"))
    return ["nothing to start — see the messages above"] if not want else []


def services(want: set[str], config_path: str, interval: int, main_root: Path,
             db: Path) -> list[Service]:
    found: list[Service] = []
    if "obs" in want:
        home = preflight.visualizer_dir()
        found.append(Service("obs", ["bun", "run", "server/index.ts"], home,
                             {"ASF_DB": str(db), "PORT": str(API_PORT)}))
        found.append(Service("ui", ["bunx", "vite"], home, {"PORT": str(API_PORT)}))
    for name in ("issues", "prs"):
        if name in want:
            found.append(Service(name, [sys.executable, RUNNER, "--config", config_path, name,
                                        "loop", "--interval", str(interval)],
                                 main_root, {"PYTHONUNBUFFERED": "1"}))
    return found


def up(cfg: FactoryConfig, config_path: str, interval: int, only: str) -> int:
    main_root = git_helper.main_root()
    db = anchor(main_root, cfg.observability.db)
    print(f"asf up — {main_root}")
    want = wanted(cfg, only)
    problems = check(cfg, want)
    if problems:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
        return 2
    if "obs" in want:
        if not db.exists():
            from .tracer import ensure_db
            ensure_db(db).close()
            print(f"  {paint(DIM, 'no trace db yet — created an empty one')}")
        home = preflight.visualizer_dir()
        if home is not None and not (home / "node_modules").is_dir():
            print("  installing the visualizer's dependencies (first run only)…")
            subprocess.run(["bun", "install"], cwd=home, check=False)

    width = max(len(name) for name in COLORS)
    lock = threading.Lock()

    def on_line(name: str, line: str) -> None:
        with lock:
            print(f"{paint(COLORS.get(name, ''), name.rjust(width))} {paint(DIM, '│')} {line}",
                  flush=True)

    started = services(want, config_path, interval, main_root, db)
    for service in started:
        _spawn(service, on_line)
    print()
    if "obs" in want:
        print(f"  trace UI   http://localhost:{UI_PORT}   (api on :{API_PORT})")
    for name in ("issues", "prs"):
        if name in want:
            print(f"  {name:<9}  polling every {interval}s")
    print(f"\n{paint(DIM, '  ctrl-c stops all of it')}\n")

    stopping = threading.Event()

    def shutdown(signum, _frame) -> None:
        if not stopping.is_set():
            stopping.set()
            print(f"\n{paint(DIM, 'stopping…')}", flush=True)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        collapsed = _supervise(started, stopping, on_line)
    finally:
        for service in started:
            _stop(service)
        print(paint(DIM, "stopped"))
    return 1 if collapsed else 0


def _supervise(started: list[Service], stopping: threading.Event, on_line) -> bool:
    """Restart what dies; a crash loop (three restarts inside a minute) is
    given up on and said, and the rest keeps running. True when nothing is left."""
    while not stopping.is_set():
        time.sleep(0.4)
        alive = False
        for service in started:
            if service.give_up:
                continue
            if service.proc and service.proc.poll() is None:
                alive = True
                continue
            code = service.proc.returncode if service.proc else -1
            service.restarts = service.restarts + 1 if time.monotonic() - service.started_at < 60 else 0
            if service.restarts > 3:
                on_line(service.name, f"exited ({code}) and keeps exiting — giving up on it; "
                                      f"the rest keeps running")
                service.give_up = True
                continue
            on_line(service.name, f"exited ({code}) — restarting")
            time.sleep(min(2 ** service.restarts, 15))
            if stopping.is_set():
                return False
            _spawn(service, on_line)
            alive = True
        if not alive:
            on_line("up", "every service has given up — nothing left to supervise")
            return True
    return False


# ── status ───────────────────────────────────────────────────────────────────

def _age(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def status(cfg: FactoryConfig) -> int:
    """One screen: what is watching, what is running, what is left behind —
    all from files, so it answers on a machine with no trace db."""
    main_root = git_helper.main_root()
    db = anchor(main_root, cfg.observability.db)
    sessions = artifacts.sessions_root(main_root, cfg.defaults.data_dir)
    rows = artifacts.watcher_states(artifacts.watchers_dir(main_root, cfg.defaults.data_dir))
    print(f"repo:      {main_root}")
    print(f"db:        {db}{'' if db.exists() else '  (no runs yet)'}\n")
    print("watchers")
    for kind, enabled in (("issues", cfg.issues.enabled), ("prs", cfg.pull_requests.enabled)):
        row = rows.get(kind)
        if not row:
            state = "never started here" if enabled else "off (enabled: false)"
            print(f"  {kind:<7} {state}" + ("        — `asf up`" if enabled else ""))
            continue
        if not _pid_alive(int(row.get("pid") or 0)):
            age = _age(row.get("last_poll_at"))
            print(f"  {kind:<7} stopped {age}" if row.get("status") == "stopped"
                  else f"  {kind:<7} not running (last {row.get('status')}, {age})")
            continue
        note = row.get("note") or ""
        print(f"  {kind:<7} {row.get('status'):<8} pid {row.get('pid')}  "
              f"last poll {_age(row.get('last_poll_at'))}{'  · ' + note if note else ''}")
    live = {adw_id: pid for adw_id, pid in artifacts.running_pids(sessions).items()
            if _pid_alive(pid)}
    print(f"\nruns in flight: {len(live)}")
    for adw_id, pid in sorted(live.items()):
        print(f"  {adw_id}  pid {pid}")
    waiting = artifacts.waiting_sessions(sessions)
    print(f"waiting for a human: {len(waiting)}" + ("   (`asf pending`)" if waiting else ""))
    for adw_id, what in sorted(waiting.items()):
        print(f"  {adw_id}  gate {what.gate} round {what.round}  since {_age(what.since)}")
    try:
        trees = worktree.inventory(main_root, cfg.worktree, str(sessions))
        if trees:
            print(f"\nworktrees: {len(trees)}   (`asf worktrees list` for detail)")
    except Exception:                                   # noqa: BLE001 — a footnote
        pass
    return 0
