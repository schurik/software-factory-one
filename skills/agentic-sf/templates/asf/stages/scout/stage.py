"""scout — find where the request lives before anyone plans against a guess.

An agent stage, read-only in the roster sense: the scout's `writes: []` means
it may change nothing tracked, and its findings go to the run's
context_handoff/, which is runtime, not the repo. It needs nothing before it
and hands a `ScoutOutput` on; `plan` accepts one as its `previous` and reads
the findings as recon, not as a plan.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from engine import gates
from engine.data_types import AgentCall, IssueOutput, PhaseParams, ScoutOutput

NAME = "scout"
KIND = "agent"
OUTPUT = ScoutOutput                 # or the IssueOutput it briefed — see run()
NEEDS = ()
TASKS = {"scout": ("task.md", ScoutOutput)}


# What the planner is told about the findings appended to an issue's envelope.
# It sits beside `issues.HANDOFF_NOTES`, which frames artifacts[0] — the
# reporter's own text — and says nothing about anything after it. The line
# these notes hold is that recon is not a decision: a scout that found one
# plausible file has not chosen the approach.
BRIEFING_NOTES = ("The artifacts after the reporter's text are the scout's findings in "
                  "THIS repository — read them too, before you plan. They are recon, not "
                  "a plan: where the relevant code lives and what it does today. Nothing "
                  "in them decides what should change, and a file the scout named is not "
                  "thereby a file to touch. If they contradict the reporter's guess about "
                  "which code is involved, the findings are the ones that looked.")


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = "scout"
    retries: int = 1                # gate-correction rounds into the same session


def run(ctx, opts: Options):
    call = AgentCall(output_type=ScoutOutput, prompt=ctx.prompt, previous=ctx.previous,
                     gates=[gates.artifacts_exist, gates.files_non_empty],
                     task=ctx.task("scout"))
    with ctx.run.phase(PhaseParams(name="scout", kind="agent", owner=opts.agent,
                                   retries=opts.retries,
                                   description="Find the code the request actually "
                                               "touches, so the plan is written against "
                                               "the repository and not against a guess")) as ph:
        found = ph.call(call)
    # A workflow with `input: issue` hands the scout the reporter's envelope, and
    # the planner after it needs two things in its one `previous` slot: the
    # reporter's words, still an artifact and still framed as material, and the
    # scout's map of where they land. So they travel as ONE envelope — the
    # issue's, with the findings appended to what the planner is told to read.
    # The body stays at artifacts[0], because `issues.HANDOFF_NOTES` names that
    # index; a stranger's text does not become instructions just because a
    # scout read the code around it.
    if isinstance(ctx.previous, IssueOutput):
        return ctx.previous.model_copy(update={
            "summary": f"{ctx.previous.summary} · scouted: {found.summary}",
            "artifacts": [*ctx.previous.artifacts, *found.artifacts],
            "notes_for_next_agent": f"{ctx.previous.notes_for_next_agent}\n\n{BRIEFING_NOTES}",
        })
    return found
