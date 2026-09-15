#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""install — stamp the agentic-sf factory from the skill into the cwd. Idempotent.

Usage:
    uv run <skill>/scripts/install.py [--harness claude_code|pi] [--force]
                                     [--no-detect-quality]

Stamps `asf/` — the engine, the stage vocabulary, the starter agents and
workflows, the runner — plus a factory.yaml assembled for the chosen harness,
that harness's `.env.sample`, the justfile, and the .gitignore entries. Existing files are
skipped unless --force. ONE FILE IS NEVER OVERWRITTEN even then: factory.yaml
is the operator's; under --force a changed render lands beside it as `.new`.

Stdlib only: this runs under `uv run` with no dependencies.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _detect                                     # noqa: E402  (path set above)

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_ROOT / "templates"
HARNESSES = TEMPLATES / "harnesses"

GITIGNORE_ENTRIES = [
    "asf/data/",
    ".env",
    ".asf-worktrees/",
    "__pycache__/",
    "*.pyc",
]


def harness_names() -> list[str]:
    return sorted(d.name for d in HARNESSES.iterdir()
                  if d.is_dir() and (d / "defaults.yaml").is_file())


def about(harness: str) -> tuple[str, str]:
    path = HARNESSES / harness / "about.md"
    if not path.is_file():
        return harness, ""
    head, _, body = path.read_text().partition("\n")
    return head.strip(), body.strip()


def choose(requested: str | None) -> str:
    available = harness_names()
    if requested:
        if requested not in available:
            sys.exit(f"unknown harness {requested!r} — this skill ships: "
                     f"{' | '.join(available)}")
        return requested
    if not sys.stdin.isatty():
        sys.exit("which harness? pass --harness <name> — this skill ships: "
                 f"{' | '.join(available)}\n(nothing to ask on: stdin is not a terminal)")
    print("Which coding-agent harness should this factory's agents run on?\n")
    for index, name in enumerate(available, start=1):
        print(f"  {index}. {about(name)[0]}")
    print()
    while True:
        try:
            answer = input(f"harness [{'/'.join(available)}]: ").strip()
        except EOFError:
            sys.exit("\nno answer — nothing was stamped")
        if answer in available:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(available):
            return available[int(answer) - 1]
        print(f"  not one of {' | '.join(available)} — try again, or Ctrl-C to abort")


def render_config(harness: str) -> str:
    head = (HARNESSES / harness / "defaults.yaml").read_text().rstrip() + "\n"
    rest = (TEMPLATES / "factory.yaml").read_text().rstrip() + "\n"
    return head + rest


def stamp(src: Path, dest: Path, force: bool, stamped: list, skipped: list) -> None:
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.name == "__pycache__":
                continue
            stamp(child, dest / child.name, force, stamped, skipped)
        return
    if dest.exists() and not force:
        skipped.append(str(dest))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    stamped.append(str(dest))


def write_config(harness: str, dest: Path, force: bool,
                 stamped: list, skipped: list, notes: list) -> None:
    fresh = render_config(harness)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(fresh)
        stamped.append(str(dest))
        return
    if not force or dest.read_text() == fresh:
        skipped.append(str(dest))
        return
    proposed = dest.with_name(dest.name + ".new")
    proposed.write_text(fresh)
    notes.append((str(dest), str(proposed)))


JUSTFILE_MARK = "# agentic-sf recipes."


def stamp_justfile(root: Path, force: bool, stamped: list, skipped: list) -> str:
    """`justfile` when the repo has none or ours; `asf.justfile` beside a
    foreign one — a repository's own recipes are not the factory's to overwrite."""
    target = root / "justfile"
    if target.exists() and not target.read_text().startswith(JUSTFILE_MARK):
        target = root / "asf.justfile"
        stamp(TEMPLATES / "justfile", target, force, stamped, skipped)
        return (f"this repo already has a justfile, so the recipes went to {target.name}: "
                f"`just -f {target.name} <recipe>`, or import it from the other one")
    stamp(TEMPLATES / "justfile", target, force, stamped, skipped)
    return ""


def ensure_gitignore(root: Path, stamped: list) -> None:
    gitignore = root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gitignore.open("a") as f:
            f.write("\n# agentic-sf runtime\n" + "\n".join(missing) + "\n")
        stamped.append(f"{gitignore} (+{len(missing)} entries)")


def ensure_env(root: Path, sample: Path, stamped: list, notes: list) -> bool:
    """Whether `.env` ends up carrying a usable ASF_SKILL.

    False is not cosmetic: unset, `just up` and `just obs` start without the
    trace UI and `just uninstall` cannot find the skill at all — so main()
    says it loudly rather than leaving it to `doctor`.
    """
    env = root / ".env"
    if not env.exists() and sample.exists():
        env.write_text(sample.read_text())
        stamped.append(str(env))
    if not env.exists():
        return False
    lines = env.read_text().splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("ASF_SKILL="):
            continue
        current = line.split("=", 1)[1].strip().strip("\"'")
        if not current:
            lines[index] = f"ASF_SKILL={SKILL_ROOT}"
            env.write_text("\n".join(lines) + "\n")
            notes.append(f"ASF_SKILL={SKILL_ROOT}  (written into .env)")
        elif Path(current).resolve() != SKILL_ROOT:
            notes.append(f"ASF_SKILL in .env is {current}, but this install ran from "
                         f"{SKILL_ROOT} — left as it is")
        return True
    with env.open("a") as f:
        f.write(f"\nASF_SKILL={SKILL_ROOT}\n")
    notes.append(f"ASF_SKILL={SKILL_ROOT}  (appended to .env)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness", help="which coding-agent harness the agents run on; "
                                          "asked interactively if omitted")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--no-detect-quality", action="store_true",
                        help="leave every quality.py block a placeholder")
    args = parser.parse_args()
    harness = choose(args.harness)
    root = Path.cwd()
    stamped, skipped, notes, config_notes = [], [], [], []

    stamp(TEMPLATES / "asf", root / "asf", args.force, stamped, skipped)
    write_config(harness, root / "asf" / "factory.yaml", args.force, stamped, skipped,
                 config_notes)
    stamp(HARNESSES / harness / "env.sample", root / ".env.sample", args.force, stamped, skipped)
    justfile_note = stamp_justfile(root, args.force, stamped, skipped)
    ensure_gitignore(root, stamped)
    skill_in_env = ensure_env(root, root / ".env.sample", stamped, notes)

    quality_py = root / "asf" / "engine" / "quality.py"
    detecting = not args.no_detect_quality and str(quality_py) in stamped
    detected = _detect.apply(quality_py, _detect.detect(root)) if detecting else []

    print(f"agentic-sf installed into {root} on the {harness} harness")
    print(f"  stamped: {len(stamped)} file(s)")
    for s in stamped:
        print(f"    + {s}")
    if skipped:
        print(f"  skipped (already exist, use --force to overwrite): {len(skipped)}")
    for mine, proposed in config_notes:
        print("\n  YOUR CONFIG WAS NOT TOUCHED — a fresh render is beside it:")
        print(f"    yours: {mine}\n    new:   {proposed}")
    print(f"\nthe skill is here: {SKILL_ROOT}")
    for note in notes:
        print(f"  {note}")
    if justfile_note:
        print(f"  {justfile_note}")
    _, steps = about(harness)
    if steps:
        print(f"\nbefore the first run ({harness}):\n\n{steps}")
    if detected:
        print("\nquality commands detected from this repo — check them in asf/engine/quality.py:")
        for note in detected:
            print(f"    {note}")
    if detecting:
        unwired = [b for b in _detect.BLOCKS if b not in {n.split(':')[0] for n in detected}]
        if unwired:
            print(f"\nstill unwired: {', '.join(unwired)} — a verify stage that names one "
                  f"of these FAILS rather than passing.\n    write the real argv into "
                  f"asf/engine/quality.py")
    if not skill_in_env:
        print(f"\n  ! NO .env, SO ASF_SKILL IS UNSET — `just up` and `just obs` will start "
              f"the watchers\n    without the trace UI, and `just uninstall` cannot find "
              f"the skill.\n    write it yourself:  echo 'ASF_SKILL={SKILL_ROOT}' >> .env")
    print("\nnext:  just doctor        then  just do \"<prompt>\"   (or: uv run asf/asf.py …)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
