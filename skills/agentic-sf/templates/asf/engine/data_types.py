"""Concrete data types for the asf engine.

RULE (four-param rule): any function that takes more than 4 parameters takes
ONE of these objects instead. AgentCall and PhaseParams are the pattern.

Every agent call declares a concrete output type — an EnvelopeBase subclass —
that its final JSON response is parsed against. No untyped handoffs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Type

from pydantic import BaseModel, Field, ValidationInfo, field_validator

PhaseKind = Literal["engineer", "agent", "code"]
PhaseStatus = Literal["queued", "running", "success", "fail", "waiting"]

# Wall clock for one agent turn, unless the roster says otherwise. Generous on
# purpose — a builder working a real change legitimately runs for many minutes,
# and a limit that fires on honest work would be turned off within a day. What
# it catches is the other shape: a turn that emits nothing at all, forever.
# `0` anywhere this is used means no limit. See engine/limits.py.
DEFAULT_AGENT_TIMEOUT_SECONDS = 1800

# Words that carry no intent, so a description made only of the phase name plus
# some of these is still only the phase name. `commit_plan: "Commit the plan"`
# is the example rule 7 has always cited — and it passed a check that compared
# the two strings verbatim, because "commit the plan" is not "commit plan".
DESCRIPTION_FILLER = {"a", "an", "the", "this", "its", "it", "of", "to", "for"}


def _significant_words(text: str) -> list[str]:
    """The words a description would still say something with. Order kept."""
    return [word for word in re.findall(r"[a-z0-9]+", text.casefold())
            if word not in DESCRIPTION_FILLER]


# ── Phases ────────────────────────────────────────────────────────────────────

class PhaseParams(BaseModel):
    """Everything run.phase() needs. Passed as one object, never loose params."""

    name: str                       # short id, unique within the run: "plan", "build"
    kind: PhaseKind                 # which lane the block renders in
    owner: str                      # engineer's name, "git", or an agent name from config
    description: str                # REQUIRED: what this phase does and why — see below
    retries: int = 0                # agent phases: gate-failure retries via continue

    @field_validator("description")
    @classmethod
    def _description_must_be_earned(cls, value: str, info: ValidationInfo) -> str:
        """A phase name identifies; a description explains. Both are required.

        The description is the only sentence the trace, the console, and the
        phase block in the UI ever show about intent — everything else is ids,
        statuses, and timings. `commit_plan: "Commit the plan"` tells a reader
        nothing they could not already see, so an echo is rejected the same way
        a blank one is. This is a construction-time error on purpose: it fires
        before the phase opens, not after a run is already in the trace.
        """
        text = " ".join(value.split())
        name = str(info.data.get("name", "?"))
        if not text:
            raise ValueError(
                f"phase {name!r}: description is required — one sentence on what this "
                f"phase does and why. It is what the trace and the UI show.")
        if _significant_words(text) == _significant_words(name.replace("_", " ")):
            raise ValueError(
                f"phase {name!r}: description {text!r} only restates the phase name — "
                f"say what it does and why instead.")
        return text


class Phase(BaseModel):
    """The persisted phase record — PhaseParams plus lifecycle."""

    phase_id: str
    adw_id: str
    seq: int
    params: PhaseParams
    status: PhaseStatus = "fail"    # success must be earned
    attempt: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Envelopes (agent output types) ───────────────────────────────────────────

class EnvelopeBase(BaseModel):
    """Base of every agent's final JSON response. Output types extend this."""

    status: Literal["success", "fail"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""


class GenericOutput(EnvelopeBase):
    pass


class PlanOutput(EnvelopeBase):
    # Subject for committing the PLAN — the spec file the planner wrote, not the
    # implementation it describes. Each agent's commit_message covers its own
    # work product, so a chain that commits per step never reuses one agent's
    # words for another agent's diff.
    commit_message: str = ""


class BuildOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""        # consumed by the git commit phase


class ScoutFinding(BaseModel):
    file: str
    note: str = ""


class ScoutOutput(EnvelopeBase):
    findings: list[ScoutFinding] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    """One thing the request (or plan) asked for, and whether it is there."""

    requirement: str                # the ask, in the requester's words
    met: bool
    evidence: str = ""              # where it lives, or what is missing


class ReviewOutput(EnvelopeBase):
    """Confirmation that what was built is what was asked for — not a test run."""

    approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)   # what must change before approval


class DocumentOutput(EnvelopeBase):
    """Where the write-up of a completed change landed."""

    document_path: str = ""         # the doc in the repo, e.g. app_docs/<adw_id>_<slug>.md
    documented_files: list[str] = Field(default_factory=list)
    commit_message: str = ""


# ── Deterministic quality blocks ─────────────────────────────────────────────

QualityArea = Literal["frontend", "backend"]
QualityOperation = Literal["lint", "typecheck", "build"]


class QualityCheckSpec(BaseModel):
    """One deterministic quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    argv: list[str]
    timeout_seconds: int = 120


class QualityCheckResult(BaseModel):
    """Captured evidence from one quality command."""

    name: str
    area: QualityArea
    operation: QualityOperation
    command: str
    returncode: int
    passed: bool
    duration_seconds: float
    output_artifact: str
    # The tail of stdout+stderr, verbatim and unparsed. A failure has to travel
    # back to the builder as an envelope, and the builder cannot open a log file
    # it was never handed — so the evidence rides along. Deliberately raw: every
    # runner formats failures differently and a generic parser would be
    # confidently wrong. The full log is always at output_artifact.
    output_tail: str = ""


class QualityResult(BaseModel):
    """Aggregate result from a quality block: every check it ran, and the verdict."""

    passed: bool
    checks: list[QualityCheckResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


# ── Preflight (what would fail later, asked now) ─────────────────────────────

FindingLevel = Literal["ok", "warn", "fatal"]


class Finding(BaseModel):
    """One preflight answer: what was asked, how it went, and how to fix it.

    `fix` is not optional decoration. A finding that names a problem without
    naming the command that ends it just moves the search from mid-chain to
    startup, and the whole point of asking early is that the answer is
    actionable while nothing has been spent yet.
    """

    check: str                      # short id: "git", "credentials: planner"
    level: FindingLevel = "ok"
    detail: str = ""                # what is true right now
    fix: str = ""                   # what to do about it, concretely

    @property
    def line(self) -> str:
        return f"{self.check}: {self.detail}" if self.detail else self.check


# ── Change capture (git diff, deterministic) ─────────────────────────────────

class ChangeCapture(BaseModel):
    """Everything documentation.capture() needs. One object, never loose params."""

    base: str = "main"              # the ref the work is measured against
    max_diff_lines: int = 2000      # the diff artifact is truncated past this
    include_untracked: bool = True  # a brand-new file is part of the change


class BaseRef(BaseModel):
    """The commit a change is measured from, and why that one.

    `reason` is the line the trace shows. A diff is only as trustworthy as the
    thing it was taken against, so the ADW records that choice instead of
    leaving the reader to infer it.
    """

    ref: str                        # what was asked for: "main", or a pinned sha
    commit: str                     # the commit actually diffed against
    reason: str = ""

    @property
    def label(self) -> str:
        """Display form — a named ref as itself, a pinned raw sha shortened."""
        if len(self.ref) == 40 and all(c in "0123456789abcdef" for c in self.ref):
            return self.ref[:7]
        return self.ref


class ChangeSet(BaseModel):
    """What changed since the base commit — pure git facts, no judgement."""

    base: BaseRef
    files: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""                  # `git diff --stat` output, verbatim
    diff_path: str = ""             # the full diff, written into context_handoff/
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not (self.files or self.untracked)


class ChangesOutput(EnvelopeBase):
    """A ChangeSet shaped as an envelope so an agent can be handed it directly.

    Same adapter idea as VerifyOutput: code computes the diff, the documenter
    consumes it through the one door every agent handoff uses.
    """

    base: str = ""                  # "<ref> @ <commit> — <reason>"
    changed_files: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    stat: str = ""
    diff_path: str = ""             # read this for the full diff


class VerifyOutput(EnvelopeBase):
    """A deterministic result, shaped as an envelope so an agent can consume it.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door a tester agent's report used to —
    the ADW script is the only thing that knows the difference.
    """

    passed: bool = False
    failures: list[str] = Field(default_factory=list)


class IssueOutput(EnvelopeBase):
    """A tracked work item, shaped as an envelope so an agent can consume it.

    Same adapter idea as VerifyOutput and ChangesOutput: code fetches the issue,
    and whichever agent comes next receives it through the one door every agent
    handoff uses. WHICH agent is the ADW's business, not this type's — a scout
    triaging it, a planner specifying it, or a refinement agent enriching it
    before either are all the same handoff, and none of them needs a variant.

    The BODY IS NOT A FIELD, and that is deliberate twice over. Envelopes are
    persisted whole into `envelopes.payload_json`, and an issue body can be a
    screenshot-laden novel. More importantly, a body that arrives as a path in
    `artifacts` is visibly MATERIAL THE AGENT READS rather than INSTRUCTIONS THE
    AGENT RECEIVED — the reporter is not the operator, and the framing is the
    cheapest part of keeping that true. `issues.as_envelope` says so out loud in
    `notes_for_next_agent`.
    """

    number: int = 0
    url: str = ""
    title: str = ""
    labels: list[str] = Field(default_factory=list)
    author: str = ""


class PullRequestOutput(EnvelopeBase):
    """Open review feedback on a pull request, shaped as an envelope.

    The same adapter as IssueOutput, one step later in the life of a branch: an
    issue is what STARTS a session, review threads are what a session hears back
    once its work is under review. Both are text written by someone who is not
    the operator, so both arrive the same way and with the same framing.

    THE THREADS ARE NOT A FIELD, for the reason IssueOutput gives about bodies
    and for one more: a review thread is a conversation, and the part that
    matters to the builder is which file and line it hangs on. That reads as a
    document and not as a JSON blob, so it is written to `artifacts[0]` and the
    envelope carries only what an ADW needs to decide with — how many threads
    there are, and where the pull request is.
    """

    number: int = 0
    url: str = ""
    title: str = ""
    branch: str = ""                # headRefName — the session's own branch
    base_ref: str = ""
    thread_count: int = 0           # how many are actionable, not how many exist


# ── Agent calls ──────────────────────────────────────────────────────────────

class GateCheck(BaseModel):
    """One thing a gate looked at, and what it found.

    `note` is the evidence — "exists, 2.1KB", "exit 0", "not in the diff". On a
    failed check it doubles as the reason, so it is what the agent is told.
    """

    item: str                       # what was checked: a path, a command, a test
    ok: bool
    note: str = ""


class GateReport(BaseModel):
    """What every gate returns: the checks it ran. Violations are derived.

    Authoring stays a one-liner per item — `report.check(...)` appends and
    returns self, so a gate is a loop and a return.
    """

    checks: list[GateCheck] = Field(default_factory=list)

    def check(self, item: str, ok: bool, note: str = "") -> "GateReport":
        self.checks.append(GateCheck(item=item, ok=ok, note=note))
        return self

    @property
    def violations(self) -> list[str]:
        return [f"{c.item}: {c.note or 'failed'}" for c in self.checks if not c.ok]

    @property
    def passed(self) -> bool:
        return not self.violations


class AgentCall(BaseModel):
    """One agent invocation: prompt in, typed envelope out, gates verified."""

    model_config = {"arbitrary_types_allowed": True}

    output_type: Type[EnvelopeBase]
    prompt: str
    previous: Optional[EnvelopeBase] = None
    gates: list[Callable] = Field(default_factory=list)   # gate(envelope, run) -> list[str]
    # The task file this call renders as the user prompt — resolved by the
    # stage (workflow-local override, else the stage's default). A call without
    # one falls back to the agent's `prompt_engineering.user`, and a call with
    # neither is refused before anything spawns.
    task: Optional[str] = None
    # Extra {{placeholders}} for the task file, beyond the three every task
    # gets (prompt, previous_envelope, context_handoff_dir). Facts, not prose:
    # a stage passes `diff_path` or `issue_number`; the words live in the task.
    variables: dict[str, str] = Field(default_factory=dict)


# ── Human-in-the-loop (engine/hitl.py) ──────────────────────────────────

Verdict = Literal["approve", "reject", "abort"]


class Decision(EnvelopeBase):
    """What a human said at a gate, in the shape the next agent already reads.

    An ENVELOPE, so `previous=decision` hands a rejection to the agent that
    produced the artifact with no new plumbing: `notes_for_next_agent` is the
    human's notes, and `summary` says who decided what. `status` is always
    "success" — the decision happened; whether the WORK passed is `verdict`.

    `subject_digest` names exactly what was decided on: a hash of the artifact
    files at the moment the human was asked. A decision whose digest no longer
    matches the subject in front of the run is refused, so a stale
    `just approve` from an earlier round can never wave a changed plan through.
    """

    status: Literal["success", "fail"] = "success"
    gate: str
    round: int = 1
    verdict: Verdict
    notes: str = ""
    by: str = ""                    # engineer name, forge login, or "policy"
    channel: str = ""               # terminal | cli | auto
    subject_digest: str = ""
    decided_at: str = ""
    # Set the moment a run ACTS on this decision. A resumed run re-walking a
    # round it already walked honours a consumed decision without the digest,
    # the way replay honours a recorded envelope: the subject has legitimately
    # moved on (a revise rewrote the plan in place), and the human already saw
    # this one. The digest guards decisions nobody has acted on yet.
    consumed_at: str = ""

    def model_post_init(self, _context: Any) -> None:
        if not self.summary:
            who = self.by or "someone"
            self.summary = (f"{self.verdict} by {who}"
                            + (f": {self.notes}" if self.notes else ""))
        if not self.notes_for_next_agent:
            self.notes_for_next_agent = self.notes

    @property
    def approved(self) -> bool:
        return self.verdict == "approve"


class Subject(BaseModel):
    """What a gate shows the human, and what its digest is taken over."""

    gate: str
    round: int = 1
    summary: str = ""               # the envelope's own one-liner
    paths: list[str] = Field(default_factory=list)   # absolute, or relative to repo_root
    notes: str = ""                 # the producing agent's notes_for_next_agent


class Gate(BaseModel):
    """One human gate at a call site: what to show, and how to revise on reject.

    `call` is the agent call to repeat when the human rejects — the SAME output
    type and gates, with `previous=` replaced by the decision. None makes the
    gate approve/abort only, which is what a gate after a code phase is.
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str                       # the id policy keys on: "plan", "integrate"
    owner: str = ""                 # the agent that revises; "" = nobody can
    call: Optional[AgentCall] = None
    retries: int = 1                # gate-correction retries on the revise phase
    description: str = ""           # for the approve phase; a default is composed
    paths: list[str] = Field(default_factory=list)   # subject; default = envelope.artifacts


class WaitingFor(BaseModel):
    """What a suspended session is waiting on — `run.json.waiting_for`."""

    gate: str
    round: int = 1
    phase_id: str = ""
    phase_name: str = ""
    since: str = ""
    subject_digest: str = ""
    paths: list[str] = Field(default_factory=list)
    summary: str = ""
    notes: str = ""


# ── Config ───────────────────────────────────────────────────────────────────

class PromptEngineering(BaseModel):
    """Where an agent's words come from. Identity is the roster's; the task is
    the stage's.

    `system` is the agent's identity, one file per agent under asf/agents/.
    `system_append` is what a WORKFLOW adds to it — never a replacement, so five
    workflows cannot quietly grow five builders. `user` is optional here on
    purpose: the task an agent is given belongs to the stage that calls it
    (`AgentCall.task`), and a roster agent no longer carries a user.md of its
    own. It stays as a fallback for a call that names no task.
    """

    system: str                     # path to agent.md (its body is the identity)
    system_append: list[str] = Field(default_factory=list)   # workflow-local additions
    user: str = ""                  # optional fallback task, if a call names none


class AgentConfig(BaseModel):
    name: str
    # The harness this agent runs on — a name in engine/harnesses. A plain
    # str, not a Literal: a new harness is one module plus one template
    # directory, and a closed list here would make it a third edit in shared
    # code. `agents.validate()` checks the name against the registry and lists
    # what exists, which is the error a Literal would have given anyway.
    harness: str = "pi"
    model: str = "google/gemini-3.6-flash"
    thinking: str = "medium"        # off | minimal | low | medium | high | xhigh | max
    color: str = ""                 # hex swatch for this agent's lane in the UI
    purpose: str = ""
    prompt_engineering: PromptEngineering
    harness_engineering: list[str] = Field(default_factory=list)
    tools: Optional[list[str]] = None    # allowlist; None = all tools usable
    # What this agent may MODIFY in the repo, enforced in code after every call
    # (see engine/permissions.py). `tools` cannot express this: `bash` runs
    # anything and `write` reaches any path, so an agent's capability list is a
    # statement of intent that nothing checks.
    #   None  -> unrestricted, except the roster-wide `protected_files` paths
    #   []    -> read-only: may modify nothing tracked
    #   [...] -> only these. A trailing "/" means a directory prefix; a "*"
    #            makes it a glob; anything else is an exact path.
    writes: Optional[list[str]] = None
    # This agent's settings for ITS harness, already merged over the defaults
    # block for that harness. Untyped here on purpose: the shape belongs to the
    # harness module (`harnesses.<name>.Options`), which parses it during
    # validation and again when it builds the command line — so a harness owns
    # its own options without data_types.py having to know they exist.
    harness_options: dict[str, Any] = Field(default_factory=dict)
    # Wall clock for ONE turn of this agent — a send, a JSON re-prompt, a gate
    # correction — each measured on its own. 0 disables it. Inherited from
    # `defaults.timeout_seconds` like every other per-agent setting; an agent
    # whose work is genuinely long (a builder on a big suite) raises its own.
    # Not a spend limit: that is `budget:`, and it is per session.
    timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS


class ConfigDefaults(BaseModel):
    harness: str = "pi"
    model: str = "google/gemini-3.6-flash"
    thinking: str = "medium"
    color: str = ""
    # Roster-wide wall clock per agent turn; any agent may override with its
    # own, and `0` restores the unbounded behaviour every version before this
    # one had.
    timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS
    harness_engineering: list[str] = Field(default_factory=list)
    tools: Optional[list[str]] = None    # roster-wide allowlist; None = all tools usable
    # Keyed BY HARNESS NAME here, because a mixed roster needs a block per
    # harness: {"claude_code": {...}, "pi": {...}}. An agent inherits only the
    # block for the harness it runs on, key by key — see agents.load_config.
    harness_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Off-limits to every agent that has not named them in its own `writes`.
    # The factory's own code is the default: an agent must not be able to edit
    # the machinery that decides whether its work passed.
    protected_files: list[str] = Field(default_factory=lambda: [
        "asf/engine/", "asf/stages/", "asf/workflows/", "asf/agents/",
        "asf/factory.yaml", "asf/asf.py",
    ])
    data_dir: str = "asf/data"


IntegrationMode = Literal["none", "merge", "pr"]


class IntegrationConfig(BaseModel):
    """How a run's branch gets back to the base branch. Convention, not code.

    Repositories disagree about merge vs. rebase, about who is allowed to move
    the base branch, and about whether a machine may do it at all — so this is
    configuration. The integration phase reads it; nothing in it is hard-coded.
    """

    mode: IntegrationMode = "merge"
    merge_flags: list[str] = Field(default_factory=lambda: ["--no-ff"])
    remote: str = "origin"                       # mode: pr — where the branch is pushed
    open_pr: bool = False                        # mode: pr — also run pr_command
    # Left as a command rather than an API call: whichever forge CLI the repo
    # uses is already authenticated in the engineer's shell, and the phase runs
    # under operator_env() so it resolves exactly as it does in their terminal.
    pr_command: list[str] = Field(default_factory=lambda: ["gh", "pr", "create", "--fill"])
    # Rendered with {adw_id}, {branch}, {base_ref} and — for an issue-triggered
    # run — {issue_number} and {issue_url}, then passed as --body ahead of that
    # run's own `Closes #<n>` line. Left empty, an issue-triggered run still
    # sends `Closes #<n>` as the whole body; a run with no issue sends nothing,
    # and pr_command decides on its own (`--fill` does).
    # NOTE: `--fill` and an explicit body are mutually exclusive in gh; the
    # integrate phase drops --fill from the command itself once there is a body.
    pr_body_template: str = ""


class WorktreeConfig(BaseModel):
    """One git worktree and one branch per run.

    A run that executes in the engineer's working tree cannot be concurrent, is
    destructive when it goes wrong, and commits whatever else was lying around.
    Isolation — not a sandbox: an agent with bash can still leave the worktree,
    which is what permissions.py is for.
    """

    enabled: bool = True
    dir: str = ".asf-worktrees"     # relative to the MAIN checkout; gitignored
    branch_prefix: str = "asf/"     # the run's branch is <prefix><adw_id>
    base_ref: str = ""               # "" = whatever the main checkout has checked out
    # A successful run's worktree is a redundant copy of a branch, so it goes.
    # A failed or killed one is where you go to see what happened, so it stays —
    # and so does any worktree with uncommitted work in it, whatever the outcome.
    keep_on_success: bool = False
    integration: IntegrationConfig = Field(default_factory=IntegrationConfig)


class BudgetConfig(BaseModel):
    """What one SESSION may spend, across every process that joins it.

    Per session and not per process, because `--adw-id` re-entry is normal: a
    chain, then `just integrate`, then a review run answering comments on the
    pull request it opened are three processes against one adw_id, and a
    ceiling that reset with each of them would bound nothing. `Run` seeds
    itself from the session's own `run.json` — never the trace db, which nothing
    in the factory reads — so a joined run starts where the last one stopped
    whether or not that db was ever written.

    Both default to 0 — no ceiling, exactly as every version before this one
    behaved. A repository that runs the factory unattended (an issue watcher,
    cron) is the one that wants them set.

    Distinct from `harness_options.claude_code.max_budget_usd`, which is one
    harness's per-CALL ceiling enforced by the CLI itself. This one is
    harness-agnostic, cumulative, and the factory's own.
    """

    max_cost_usd: float = 0.0       # 0 = no ceiling
    max_tokens: int = 0             # 0 = no ceiling


class HitlConfig(BaseModel):
    """Which gates stop for a human, and what a stopped run does.

    Placement is the ADW's (`hitl.gated(...)` at a call site names a gate);
    this decides whether a placed gate FIRES. Most specific wins: the `--hitl`
    flag, then `ASF_HITL`, then `gates` by name, then `default`.

    Off by default, so a stamped repository behaves exactly as it did.
    """

    # `on` and `off` are what a config says — and in YAML those ARE booleans,
    # so `default: off` arrives here as False. Stored as bool; the words are
    # accepted too, for a config written with quotes or by hand in Python.
    default: bool = False                # off | on
    gates: dict[str, bool] = Field(default_factory=dict)   # {"plan": on}
    wait_seconds: int = 900              # attended: prompt this long, then suspend
    max_rounds: int = 0                  # 0 = until the human approves or aborts
    # What an issue- or PR-triggered run does at an on-gate: `suspend` stops
    # and waits (there is no terminal to ask); `auto` records a policy approval
    # and continues. The safe default is the first.
    when_unattended: str = "suspend"     # suspend | auto
    # Run when a gate suspends, with the subject on stdin — a known command is
    # code. [] runs nothing.
    notify_command: list[str] = Field(default_factory=list)

    @field_validator("default", "gates", mode="before")
    @classmethod
    def _words_are_switches(cls, value: Any) -> Any:
        return ({k: _switch(v) for k, v in value.items()} if isinstance(value, dict)
                else _switch(value))

    @field_validator("when_unattended")
    @classmethod
    def _suspend_or_auto(cls, value: str) -> str:
        if value not in ("suspend", "auto"):
            raise ValueError(f"hitl.when_unattended: {value!r} is not suspend | auto")
        return value


def _switch(value: Any) -> Any:
    """`on`/`off` (and yes/no, true/false) as a bool; anything else is left for
    pydantic to refuse with its own message."""
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("on", "true", "yes"):
            return True
        if word in ("off", "false", "no"):
            return False
    return value


class ObservabilityConfig(BaseModel):
    db: str = "asf/data/asf.db"
    poll_ms: int = 500


class IssueStates(BaseModel):
    """The label state machine. The flip from `queued` IS the lock.

    No queue and no state file: the watcher claims an issue by moving its label,
    which is atomic at the forge and visible to humans in the place they already
    look. Two watchers racing the same issue means one of them loses the flip.
    """

    queued: str = "asf:queued"
    running: str = "asf:running"
    done: str = "asf:done"
    failed: str = "asf:failed"


class IssuesConfig(BaseModel):
    """Where work items come from, and which chain each label asks for.

    Commands rather than API calls, for the reason `pr_command` already gives:
    whichever forge CLI the repo uses is installed and authenticated in the
    engineer's shell already, and everything here runs under operator_env().
    """

    enabled: bool = False
    # WHICH REPO the watcher watches. Empty resolves ONCE at startup from the
    # origin remote of the main checkout — never left to each command's cwd,
    # because cron has an arbitrary working directory and a watcher that
    # silently polls the wrong project looks exactly like one with nothing to
    # do. A tracker that is not the forge has no remote to infer from and must
    # set this. It is the same normalised identity the trace records.
    project: str = ""
    fetch_command: list[str] = Field(default_factory=lambda: ["gh", "issue", "view"])
    list_command: list[str] = Field(default_factory=lambda: ["gh", "issue", "list"])
    comment_command: list[str] = Field(default_factory=lambda: ["gh", "issue", "comment"])
    state_command: list[str] = Field(default_factory=lambda: ["gh", "issue", "edit"])
    # label -> ADW script. The watcher routes on this; no ADW knows about it.
    route: dict[str, str] = Field(default_factory=dict)
    states: IssueStates = Field(default_factory=IssueStates)
    # Empty = every issue author is accepted, and the human who applied the
    # routing label is the only authorization. Narrow it where anyone can label.
    trusted_authors: list[str] = Field(default_factory=list)
    max_concurrent: int = 2
    # An issue-triggered run must not be able to move the base branch. Enforced
    # in integration.integrate(), not left to whoever edits the config.
    force_pr: bool = True


class PullRequestStates(BaseModel):
    """The one label this path needs, and why it is not a state machine.

    An issue is claimed by moving a label, because nothing else at the forge
    records that a run took it. A review thread already carries that state:
    unresolved means outstanding, resolved means handled, and both are visible
    to the reviewer who wrote it. So there is no `queued`/`running`/`done` here.

    `failed` is the exception, and it exists to stop one specific loop: a run
    that ends red leaves its threads unresolved, which is exactly the condition
    the watcher launches on — so without a mark it would relaunch the same
    failing run every poll, forever. A human removing the label is the restart.
    """

    failed: str = "asf:pr-failed"


class PullRequestsConfig(BaseModel):
    """Review feedback as a run's entry point: read threads, answer them, resolve.

    The sibling of IssuesConfig one step later in a branch's life, and off by
    default for the same reason: the text driving the agents is written by
    whoever can review, not by the engineer at the keyboard.

    Commands rather than API calls, as everywhere else here — with one shape the
    issue path does not need. Review THREADS (their ids, and whether they are
    resolved) exist only in the forge's graphql API; `gh pr view --json comments`
    returns issue-level comments and review bodies, never the inline threads
    where the actual asks are. So `graphql_command` reads a pull request and
    writes back into its threads, and there is deliberately no `fetch_command`
    beside it: one query returns the state AND the threads in one consistent
    snapshot, where two commands would disagree about a pull request that was
    merged between them.
    """

    enabled: bool = False
    # WHICH REPO, resolved once — same rule and same failure mode as
    # IssuesConfig.project: a watcher that cannot name its project polls
    # nothing, and polling nothing looks exactly like having nothing to do.
    project: str = ""
    list_command: list[str] = Field(default_factory=lambda: ["gh", "pr", "list"])
    comment_command: list[str] = Field(default_factory=lambda: ["gh", "pr", "comment"])
    state_command: list[str] = Field(default_factory=lambda: ["gh", "pr", "edit"])
    graphql_command: list[str] = Field(default_factory=lambda: ["gh", "api", "graphql"])
    # Empty = every reviewer is accepted, because the ability to review this
    # repository's pull requests is itself the authorization. Narrow it where
    # that is not true — a public fork's PR can be reviewed by anyone.
    trusted_reviewers: list[str] = Field(default_factory=list)
    # Bots whose comments are never work: coverage reporters, changelog nags.
    # The factory's own comments are skipped regardless — see pull_requests.py.
    ignore_authors: list[str] = Field(default_factory=list)
    # Which workflow the review watcher launches per pull request with open
    # threads. It must declare `input: pr`; `asf check` and the watcher refuse
    # one that does not.
    workflow: str = "pr-review"
    reply_to_threads: bool = True
    resolve_threads: bool = True
    # Bounds the prompt, not the pull request. A review with eighty threads is a
    # conversation to have, not a batch to hand an agent in one go.
    max_threads: int = 20
    max_concurrent: int = 2
    # A merged or closed pull request ends its session: a still-running review
    # run is killed, and its worktree released. See scripts/pr_watch.py.
    reap_merged: bool = True
    states: PullRequestStates = Field(default_factory=PullRequestStates)


class FactoryConfig(BaseModel):
    defaults: ConfigDefaults = Field(default_factory=ConfigDefaults)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    hitl: HitlConfig = Field(default_factory=HitlConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    worktree: WorktreeConfig = Field(default_factory=WorktreeConfig)
    issues: IssuesConfig = Field(default_factory=IssuesConfig)
    pull_requests: PullRequestsConfig = Field(default_factory=PullRequestsConfig)
    agents: list[AgentConfig] = Field(default_factory=list)


# ── Workspace (where a run works, and where its record lives) ────────────────

class Workspace(BaseModel):
    """The two roots a run has, kept apart on purpose.

    `repo_root` is the tree the agents are spawned in, the gates measure, the
    permission snapshot fingerprints and the commit phases commit. `main_root`
    is the engineer's checkout, which a run must never modify — but which owns
    the one thing that has to outlive the run: `data_dir`, and with it the trace
    db, the session dir and context_handoff/. A worktree is pruned; the trace of
    what happened in it is not.

    Without a worktree (disabled, or not a git repo) both point at the same
    directory and every path below behaves exactly as it did before.
    """

    main_root: Path
    repo_root: Path
    enabled: bool = False           # False = running directly in the main checkout
    branch: str = ""                # asf/<adw_id>
    base_ref: str = ""              # what it was cut from, as asked for
    base_commit: str = ""           # ...pinned to a sha at creation
    created: bool = False           # False = re-attached to a worktree that existed

    @property
    def joined(self) -> bool:
        """True when this run re-attached to a worktree an earlier ADW created."""
        return self.enabled and not self.created


class WorktreeRequest(BaseModel):
    """Everything worktree.ensure() needs. One object, never loose params."""

    main_root: Path
    adw_id: str
    config: WorktreeConfig = Field(default_factory=WorktreeConfig)


class WorktreeInfo(BaseModel):
    """One worktree on disk, and whether anything still needs it.

    A killed run leaves its worktree behind deliberately, so "left behind" and
    "orphaned" are not the same thing — the session status is what tells them
    apart, and it lives in the trace db, not in git.
    """

    path: str
    branch: str = ""
    adw_id: str = ""
    dirty: bool = False
    status: str = "unknown"         # the run's session status, from the trace db
    prunable: bool = False          # git says the directory is gone


class RecordedPhase(BaseModel):
    """One agent phase this session already completed, as its own record kept it.

    Written to `sessions/<adw_id>/envelopes/<phase_id>.json` when the phase
    produces its envelope, and handed back by `engine/replay.py` to a
    resumed run instead of calling the agent again: the phase's name and owner
    say WHICH call it answers, `output_type` says the contract it was written
    against, and the payload is the envelope itself, verbatim.
    """

    phase_id: str                   # "<adw_id>_<seq>_<name>" — the file's name
    seq: int
    phase: str                      # the phase NAME, unique within a run
    agent: str
    output_type: str
    payload_json: str


class RunState(BaseModel):
    """What `sessions/<adw_id>/run.json` says about the session itself.

    The one thing the rest of the session directory cannot say: which process
    took this session, what argv started it, and how it ended. `events.jsonl`
    describes phases; this describes the run that opened them, which is what
    `just resume` needs to launch the same workflow a second time.

    `command` is the argv as a LIST, never a joined string — no quoting to undo,
    and no clipping, so a run started from a long inline prompt resumes as
    exactly the run it was.
    """

    adw_id: str
    workflows: list[str] = Field(default_factory=list)   # every ADW this session ran, in order
    command: list[str] = Field(default_factory=list)     # argv of the NEWEST process
    pid: int = 0
    engineer: str = ""
    status: str = "running"         # running | success | fail | waiting
    started_at: str = ""
    ended_at: str = ""
    repo_root: str = ""             # the worktree the run works in
    branch: str = ""
    trigger: str = "engineer"       # engineer | issue | pr_review
    issue_url: str = ""
    pr_url: str = ""
    # The issue this run answers, as the TRACKER addresses it. `issue_url` is
    # for humans and for the PR body; these two are what a label move needs,
    # and deriving them by parsing the url would be a guess about a forge whose
    # url shape is not this factory's to know — a tracker that is not the forge
    # (Jira, Linear) has neither the shape nor the number in the same place.
    # Written once by `Run.record_issue`, carried across every later process by
    # `artifacts.start_run`, and read by `resume.py` when a run that suspended
    # at a gate finally ends. 0 means "not an issue run", which is the default.
    issue_number: int = 0
    issue_project: str = ""
    # Set while a gate waits on a human; cleared when the decision is consumed.
    # `status == "waiting"` says the PROCESS is gone; this says why, and what
    # `just approve` would be approving.
    waiting_for: Optional[WaitingFor] = None
    # What the SESSION has spent, across every process that joined it. Here
    # rather than only in `sessions.total_tokens` because `budget:` is enforced
    # against it, and a limit that needed the db would be one the factory
    # cannot honor where the db was never written.
    total_tokens: int = 0
    total_cost: float = 0.0

    @property
    def adw_name(self) -> str:
        """The session's workflows the way the trace and the UI name them."""
        return " + ".join(self.workflows)


class RunSpec(BaseModel):
    """Everything the Run object is built from, minus the tracer it writes to."""

    model_config = {"arbitrary_types_allowed": True}

    cfg: FactoryConfig
    adw_id: str
    engineer: str
    workspace: Workspace
    # Replay this session's recorded agent phases instead of re-running them.
    # Only ever true for a run that pinned an --adw-id: there is nothing to
    # resume without the session that recorded it.
    resume: bool = False
    hitl: str = ""                  # the --hitl flag, or "" for the config's say


# ── Integration (landing a run's branch) ─────────────────────────────────────

class IntegrationRequest(BaseModel):
    """One integration attempt. `mode` empty = whatever the config says."""

    mode: str = ""
    message: str = ""               # merge commit subject; defaults to the branch
    title: str = ""                 # PR title, when the forge CLI takes one
    body: str = ""                  # PR body; overrides config.pr_body_template


class IntegrationResult(BaseModel):
    """What integration actually did — a code phase's evidence, not a claim."""

    mode: IntegrationMode = "none"
    ok: bool = False
    branch: str = ""
    base_ref: str = ""
    head: str = ""                  # the branch tip that was landed or pushed
    merged_into: str = ""
    pushed: bool = False
    pr_url: str = ""
    notes: list[str] = Field(default_factory=list)


# ── Issues (a tracked work item as a run's entry point) ──────────────────────

class IssueRef(BaseModel):
    """Which work item, in which project. `project` empty = whatever config says."""

    number: int
    project: str = ""


class IssueContext(BaseModel):
    """One fetched issue. What the forge said, plus where the body was written.

    `body_path` rather than the body itself for the same reason IssueOutput has
    no body field — see that type. Nothing downstream reads `body` off this
    object; the agent opens the file.
    """

    number: int
    project: str = ""
    url: str = ""
    title: str = ""
    labels: list[str] = Field(default_factory=list)
    author: str = ""
    state: str = ""
    body_path: str = ""             # written into context_handoff/


class IssueUpdate(BaseModel):
    """One write back to the tracker: a comment, a label move, or both."""

    number: int
    project: str = ""
    comment: str = ""
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)


class IssueResult(BaseModel):
    """What a tracker write actually did — evidence, never a claim.

    A failed write-back is NOT a failed run. The work is committed and the
    branch is kept either way; the tracker just did not hear about it, which is
    a thing a human can finish by hand. So this carries `ok` and notes rather
    than raising, exactly like IntegrationResult.
    """

    ok: bool = False
    number: int = 0
    commented: bool = False
    labels_changed: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ── Pull requests (review feedback as a run's entry point) ───────────────────

class PullRequestRef(BaseModel):
    """Which pull request, in which project. `project` empty = whatever config says."""

    number: int
    project: str = ""


class ReviewComment(BaseModel):
    """One comment inside a review thread. `comment_id` is what a reply targets."""

    comment_id: int = 0             # REST databaseId — replies POST against it
    author: str = ""
    body: str = ""
    created_at: str = ""


class ReviewThread(BaseModel):
    """One review conversation, anchored to a file and line.

    `thread_id` is the GraphQL node id, and it is the only handle that can
    resolve a thread — REST has no notion of thread state at all. That is why
    the threads are read through graphql rather than through `gh pr view`, which
    returns issue-level comments and review bodies but never the inline threads
    where the actual asks live.
    """

    thread_id: str = ""
    path: str = ""                  # the file the thread hangs on, "" for a PR-level one
    line: Optional[int] = None
    resolved: bool = False
    outdated: bool = False          # the diff moved out from under it
    comments: list[ReviewComment] = Field(default_factory=list)

    @property
    def author(self) -> str:
        """Who opened the thread — the ask's author, not the last replier."""
        return self.comments[0].author if self.comments else ""

    @property
    def last_author(self) -> str:
        return self.comments[-1].author if self.comments else ""


class PullRequestContext(BaseModel):
    """One fetched pull request, plus where its threads were written.

    `threads_path` rather than the thread text itself, for the reason
    PullRequestOutput gives. `threads` is kept in memory because the run has to
    reply to each one afterwards and needs their ids — it is not persisted into
    an envelope.
    """

    number: int
    project: str = ""
    url: str = ""
    title: str = ""
    state: str = ""                 # OPEN | MERGED | CLOSED
    draft: bool = False
    author: str = ""
    branch: str = ""                # headRefName
    base_ref: str = ""              # baseRefName
    review_decision: str = ""
    threads: list[ReviewThread] = Field(default_factory=list)
    threads_path: str = ""          # written into context_handoff/

    @property
    def merged(self) -> bool:
        return self.state.upper() == "MERGED"

    @property
    def open(self) -> bool:
        return self.state.upper() == "OPEN"


class PullRequestUpdate(BaseModel):
    """One write back to a pull request: a thread reply, a comment, a label move.

    `thread_id` addresses both thread writes — the reply and the resolve — so a
    caller that has a thread can do either without a second identifier.
    """

    number: int
    project: str = ""
    comment: str = ""               # a pull-request-level comment
    reply: str = ""                 # a reply inside the thread named below
    thread_id: str = ""             # which thread to reply in and/or resolve
    resolve: bool = False
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)


class PullRequestResult(BaseModel):
    """What a pull-request write actually did — evidence, never a claim.

    Same contract as IssueResult: a forge that did not hear about a finished run
    is not a failed run. The commits are on the branch and the branch is pushed
    either way; a reviewer who did not get a reply is something a human can
    finish by hand, and failing the run over it would throw away the work.
    """

    ok: bool = False
    number: int = 0
    commented: bool = False
    replied: bool = False
    resolved: list[str] = Field(default_factory=list)   # thread ids
    labels_changed: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ── Tracing ──────────────────────────────────────────────────────────────────

class EventRecord(BaseModel):
    """One traced event, always logged against adw_id + phase."""

    adw_id: str
    phase_id: str = ""
    type: str                       # phase_start | agent_start | tool_call | handoff | gate_pass | gate_fail | log | agent_end | phase_end | error
    name: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_id: str = ""
    tokens: Optional[int] = None
    # Spans: set both when an event covers real elapsed time (a tool call), so
    # the UI lays it out on a time axis without parsing payload JSON. Left unset,
    # the tracer stamps started_at with the moment the event was recorded.
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


# ── Coding agent interface (one shape, every harness) ────────────────────────

class AgentRequest(BaseModel):
    """Everything one non-interactive coding-agent turn needs, on any harness.

    The four-param rule already forced a request object, so adding a second
    harness was a couple of fields rather than a second signature. Fields a
    harness does not use are inert, never an error: pi ignores `resume`, and
    Claude Code ignores `extensions` (it validates `harness_engineering` its
    own way — see harnesses/claude_code.py).
    """

    prompt: str
    system_prompt: str
    model: str                      # pi: a registry pattern. claude_code: an alias or model id
    thinking: str = "medium"
    session_id: str                 # the FACTORY's id for this agent's context window
    session_dir: str                # the harness's own session store, if it keeps one
    raw_output_path: str            # JSONL stream lands here
    # The run's session runtime — data_dir/sessions/<adw_id> — which lives in
    # the MAIN checkout, OUTSIDE the worktree the agent is spawned in. It holds
    # context_handoff/, the prompt copies and this agent's envelope, so every
    # agent must be able to write it whatever its `writes:` says. A harness that
    # confines file tools to the working directory has to be told about it.
    runtime_dir: str = ""
    tools: Optional[list[str]] = None
    extensions: list[str] = Field(default_factory=list)
    cwd: str = "."                  # set from run.repo_root — the codebase root agents work in
    # Create-vs-resume. pi's --session-id is create-or-continue, so pi needs
    # neither field; Claude Code's is create-ONLY and errors on a second use,
    # so it needs both — the UUID it knows the session by, and whether that
    # session already exists.
    native_session_id: str = ""     # "" = the harness uses session_id as-is
    resume: bool = False
    # The agent's `harness_options`, verbatim. Parsed by the harness that reads
    # them (`Options(**request.options)`), never here.
    options: dict[str, Any] = Field(default_factory=dict)
    # Wall clock for THIS turn. Every harness arms `limits.Deadline` with it
    # around its read loop and raises `limits.AgentTimeout` when it fires, so a
    # hung CLI fails its phase instead of blocking the run forever. 0 = no
    # limit. It is a field rather than a harness option because a harness that
    # can hang is not a harness-specific property.
    timeout_seconds: int = 0


class AgentSession(BaseModel):
    """One agent's context window within a run, as the agent map records it.

    `session_id` is the factory's name for it and never changes; the two extra
    fields exist because Claude Code's `--session-id` is create-only. The first
    call creates, every later one resumes, and `started` is the flag that says
    which — so it has to survive the process, not just the phase.
    """

    session_id: str
    native_session_id: str = ""     # what the harness calls it; pi echoes session_id back
    started: bool = False           # the session EXISTS — resume it, do not create it


class UsageBreakdown(BaseModel):
    """Tokens and the dollars they cost, per component, summed over a call.

    Mirrors pi's `usage` shape one-for-one so the numbers reconcile with what
    pi itself reports: `input` EXCLUDES cache reads, which bill at their own
    (cheaper) rate — add them to learn the size of the prompt that was sent.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Thinking tokens. NOT a fifth component: measured across every session on
    # disk, reasoning is always <= output and the four components above always
    # sum to totalTokens, so reasoning is the thinking SHARE of output, billed
    # at the output rate. Report it nested under output, never added to it.
    reasoning_tokens: int = 0
    total_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_write_cost: float = 0.0
    total_cost: float = 0.0

    # No `add_turn` here on purpose. Each harness reports usage in its own
    # vocabulary — pi says `cacheRead`, Claude Code says
    # `cache_read_input_tokens`, and only pi breaks the cost down per component
    # — so each harness owns an adapter that BUILDS one of these and merges it
    # (`harnesses/pi.py:_turn_usage`, `harnesses/claude_code.py:_result_usage`).
    # One function taught two vocabularies is how a silently-zero column happens.

    def merge(self, other: "UsageBreakdown") -> None:
        """Add another call's usage — a phase that retries spends more than once."""
        for field in type(self).model_fields:
            setattr(self, field, getattr(self, field) + getattr(other, field))


class AgentResult(BaseModel):
    """What one coding-agent turn produced. Identical across harnesses.

    `session_id` is what the harness says the session was — pi echoes back the
    id it was handed, Claude Code reports the one it created or resumed, and
    `agents.execute` writes it into the agent map either way.
    """

    text: str = ""
    returncode: int = 0
    session_id: str = ""
    tokens: int = 0
    cost: float = 0.0
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    # Context occupancy after the LAST turn — not a sum. `tokens` bills every
    # turn; this is how full the window is right now, which is what the
    # visualizer's context bar measures against `context_window`.
    context_tokens: int = 0
    context_window: int = 0         # 0 when the registry declares no ceiling


PiResult = AgentResult              # transitional alias; prefer AgentResult
