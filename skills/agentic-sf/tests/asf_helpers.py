"""What the tests do to a stamped repo: put the roster on the fake harness,
script the agents, wire a check, run the runner as a subprocess, read the
record back."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

from engine import frontmatter      # conftest puts templates/asf on the path first

SKILL_ROOT = Path(__file__).resolve().parent.parent
INSTALL = SKILL_ROOT / "scripts" / "install.py"
DB = "asf/data/asf.db"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def install(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """The installer as a user runs it: a subprocess, from the target repo."""
    return subprocess.run([sys.executable, str(INSTALL), *args], cwd=cwd,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def asf(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """`asf/asf.py …` as a user runs it, with no terminal on stdin — so a gate
    that fires suspends instead of prompting, exactly as under a watcher."""
    return subprocess.run([sys.executable, "asf/asf.py", *args], cwd=cwd,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={**os.environ, **(env or {})})


def envelope(**fields) -> dict:
    return {"status": "success", "summary": "scripted", **fields}


def fake_roster(repo: Path, **agents: list[dict]) -> None:
    """Put factory.yaml on the fake harness and script each named agent.

    `agents` is {name: replies}; a name the starter roster lacks becomes a new
    agent directory with a one-line identity, so a test can bind whatever it
    needs.
    """
    config = repo / "asf" / "factory.yaml"
    raw = yaml.safe_load(config.read_text())
    raw["defaults"].update({"harness": "fake", "model": "fake", "tools": None,
                            "harness_options": {"fake": {}}})
    config.write_text(yaml.safe_dump(raw))
    for name, replies in agents.items():
        directory = repo / "asf" / "agents" / name
        directory.mkdir(parents=True, exist_ok=True)
        spec = directory / "agent.md"
        if spec.is_file():
            entry, identity = frontmatter.split(spec.read_text())
        else:
            entry, identity = {"purpose": f"{name}, scripted"}, f"# {name.title()}\n\nYou are {name}.\n"
        entry.update({"harness": "fake", "harness_options": {"replies": replies}})
        spec.write_text(f"---\n{yaml.safe_dump(entry)}---\n\n{identity}")


def wire(repo: Path, block: str, argv: list[str]) -> None:
    """Replace one quality block's argv in the stamped quality.py."""
    quality = repo / "asf" / "engine" / "quality.py"
    source = quality.read_text()
    pattern = re.compile(rf'argv=(_placeholder\("{block}"\)|\[[^\n]*?\]),[^\n]*')
    replaced, count = pattern.subn(f"argv={json.dumps(argv)},", source, count=1)
    assert count == 1, f"no {block} block to wire in {quality}"
    quality.write_text(replaced)


PY_CHECK = [sys.executable, "-c", "import runpy; runpy.run_path('app.py')"]


def commit_all(repo: Path, message: str = "prepare") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def write_workflow(repo: Path, name: str, spec: dict, tasks: dict[str, str] | None = None,
                   appends: dict[str, str] | None = None) -> Path:
    """A workflow directory the way an engineer would lay one out."""
    directory = repo / "asf" / "workflows" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "workflow.yaml").write_text(yaml.safe_dump({"name": name, **spec}, sort_keys=False))
    for key, text in (tasks or {}).items():
        (directory / "tasks").mkdir(exist_ok=True)
        (directory / "tasks" / f"{key}.md").write_text(text)
    for filename, text in (appends or {}).items():
        (directory / "agents").mkdir(exist_ok=True)
        (directory / "agents" / filename).write_text(text)
    return directory


TASK = """# {title}

{marker}

### prompt

{{{{prompt}}}}

### previous_envelope

{{{{previous_envelope}}}}

### context_handoff_dir

{{{{context_handoff_dir}}}}

## Report

```json
{report}
```
"""


def task_text(title: str, marker: str, report: dict) -> str:
    return TASK.format(title=title, marker=marker, report=json.dumps(report, indent=2))


BUILD_REPORT = {"status": "success", "summary": "<s>", "changed_files": ["app.py"],
                "artifacts": [], "commit_message": "<m>", "notes_for_next_agent": "<n>"}


# ── reading the record back ───────────────────────────────────────────────────

def db_rows(repo: Path, sql: str) -> list[tuple]:
    """The trace db — an ASSERTION source, never a production path."""
    connection = sqlite3.connect(f"file:{repo / DB}?mode=ro", uri=True)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def adw_id_of(result: subprocess.CompletedProcess) -> str:
    """From the workspace line every run prints first — a suspended run never
    reaches the closing banner that also carries it."""
    match = re.search(r" on asf/(\w{8}) ", result.stdout)
    assert match, result.stdout + result.stderr
    return match.group(1)


def session_dir(repo: Path, adw_id: str) -> Path:
    return repo / "asf" / "data" / "sessions" / adw_id


def run_state(repo: Path, adw_id: str) -> dict:
    return json.loads((session_dir(repo, adw_id) / "run.json").read_text())


def phase_names(repo: Path, adw_id: str) -> list[str]:
    return [row[0] for row in db_rows(
        repo, f"select name from phases where adw_id='{adw_id}' order by seq")]


# ── a forge that records ─────────────────────────────────────────────────────
#
# The forge commands are config, so a python script standing in for `gh` is a
# supported deployment, not a mock. It answers `view` from issue.json, `list`
# from listing.json, `graphql` from pr.json, prints a url for `pr-create`, and
# appends every other call to calls.json. `refuse.json` names verbs it refuses
# with exit 1, the way a real one does when it cannot do an edit.

FORGE = '''\
import json, sys
from pathlib import Path
home = Path(sys.argv[1])
verb, argv = sys.argv[2], sys.argv[3:]
refuse = home / "refuse.json"
if refuse.exists() and verb in json.loads(refuse.read_text()):
    print(f"GraphQL: {verb} is not something this forge will do", file=sys.stderr)
    sys.exit(1)
log = home / "calls.json"
entries = json.loads(log.read_text()) if log.exists() else []
if verb == "view":
    print((home / "issue.json").read_text())
elif verb == "list":
    print((home / "listing.json").read_text() if (home / "listing.json").exists() else "[]")
elif verb == "graphql":
    query = next((a for a in argv if a.startswith("query=")), "")
    if "addPullRequestReviewThreadReply" in query or "resolveReviewThread" in query:
        entries.append([verb, *argv])
        print(json.dumps({"data": {}}))
    else:
        print((home / "pr.json").read_text())
elif verb == "pr-create":
    entries.append([verb, *argv])
    print("https://forge/acme/widgets/pull/9")
else:
    entries.append([verb, *argv])
log.write_text(json.dumps(entries))
'''


def forge(repo: Path) -> list[str]:
    """Install the stand-in and return the argv prefix every forge command starts with."""
    home = repo / ".forge"
    home.mkdir(exist_ok=True)
    (home / "forge.py").write_text(FORGE)
    return [sys.executable, str(home / "forge.py"), str(home)]


def forge_calls(repo: Path) -> list[list[str]]:
    log = repo / ".forge" / "calls.json"
    return json.loads(log.read_text()) if log.exists() else []


def forge_data(repo: Path, name: str, payload) -> None:
    (repo / ".forge" / name).write_text(json.dumps(payload))


def set_config(repo: Path, **sections) -> None:
    """Merge whole sections into the stamped factory.yaml."""
    config = repo / "asf" / "factory.yaml"
    raw = yaml.safe_load(config.read_text())
    for key, value in sections.items():
        raw[key] = {**(raw.get(key) or {}), **value} if isinstance(value, dict) else value
    config.write_text(yaml.safe_dump(raw))


def with_origin(repo: Path) -> Path:
    """A bare remote beside the repo, with main pushed — what a pull request needs."""
    origin = repo.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "-u", "origin", "main")
    return origin


def issue_json(number: int = 42, author: str = "someone", labels=("asf:queued", "asf:ship"),
               body: str = "The /health endpoint returns 500.\n") -> dict:
    return {"number": number, "title": f"health check broken (#{number})", "body": body,
            "labels": [{"name": name} for name in labels], "author": {"login": author},
            "state": "OPEN", "url": f"https://forge/acme/widgets/issues/{number}"}


def pr_json(number: int, branch: str, threads: list[dict] | None = None,
            state: str = "OPEN") -> dict:
    nodes = [{"id": f"T{i}", "isResolved": False, "isOutdated": False,
              "path": t.get("path", "app.py"), "line": t.get("line", 1),
              "comments": {"nodes": [{"databaseId": i, "body": t["body"],
                                      "createdAt": "2026-01-01T00:00:00Z",
                                      "author": {"login": t.get("author", "reviewer")}}]}}
             for i, t in enumerate(threads or [], start=1)]
    return {"data": {"repository": {"pullRequest": {
        "number": number, "title": "add app.py", "url": f"https://forge/acme/widgets/pull/{number}",
        "state": state, "isDraft": False, "baseRefName": "main", "headRefName": branch,
        "reviewDecision": "", "author": {"login": "someone"},
        "reviewThreads": {"nodes": nodes}}}}}
