"""Everything a workflow directory can get wrong is found at load time.

These run `engine.workflow.load` in-process against a stamped repo, and each
asserts on the message as well as the refusal: the message is the fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engine import factory, workflow
from engine.data_types import BuildOutput

from .asf_helpers import BUILD_REPORT, fake_roster, task_text, write_workflow

QUICK = [{"implement": {"agent": "builder"}},
         {"verify": {"blocks": ["test"]}},
         {"commit": {"of": "implement"}}]


@pytest.fixture
def factory_repo(stamped: Path, monkeypatch) -> Path:
    fake_roster(stamped, planner=[{"envelope": {"status": "success"}}],
                builder=[{"envelope": {"status": "success"}}])
    monkeypatch.chdir(stamped)
    return stamped


def refused(name: str) -> str:
    with pytest.raises(SystemExit) as stop:
        workflow.load(name)
    return str(stop.value)


def test_the_shipped_workflows_load_and_name_their_agents(factory_repo):
    sdlc = workflow.load("sdlc")
    assert [s.stage.name for s in sdlc.steps] == ["plan", "implement", "verify", "commit"]
    assert sdlc.required_agents == ["builder", "planner"]
    assert sdlc.steps[0].tasks["plan"].endswith("asf/stages/plan/task.md")
    assert sdlc.steps[0].stage.tasks["plan"][1].__name__ == "PlanOutput"
    quick = workflow.load("quick")
    assert quick.required_agents == ["builder"]
    ship = workflow.load("ship")
    assert [s.stage.name for s in ship.steps] == ["scout", "plan", "commit", "implement", "verify",
                                                  "review", "commit", "document", "commit", "integrate"]
    assert ship.required_agents == ["builder", "documenter", "planner", "reviewer", "scout"]
    scout = ship.steps[0].stage
    assert scout.needs == () and scout.output.__name__ == "ScoutOutput"
    assert ship.steps[1].stage.needs == ()                            # plan takes recon or nothing
    issue = workflow.load("issue")
    assert issue.input == "issue" and [s.stage.name for s in issue.steps][:2] == ["scout", "plan"]
    review_flow = workflow.load("pr-review")
    assert review_flow.input == "pr" and review_flow.required_agents == ["builder"]
    assert review_flow.steps[0].tasks["implement"].endswith("workflows/pr-review/tasks/implement.md")
    assert review_flow.steps[2].opts.allow_clean is True
    assert sdlc.input == "prompt"
    review = ship.steps[5].stage
    assert review.tasks["review"][1].__name__ == "ReviewOutput"      # what it asks for
    assert review.tasks["revise"][1].__name__ == "BuildOutput"
    assert review.output.__name__ == "BuildOutput"                    # what it hands on


def test_the_roster_is_directories_and_factory_yaml_may_not_carry_agents(factory_repo):
    cfg = factory.load()
    assert sorted(a.name for a in cfg.agents) == ["builder", "documenter", "planner", "reviewer", "scout"]
    scout = next(a for a in cfg.agents if a.name == "scout")
    assert scout.writes == [] and scout.thinking == "low"             # read-only recon, cheap
    planner = next(a for a in cfg.agents if a.name == "planner")
    assert planner.writes == ["specs/"]
    assert planner.prompt_engineering.user == ""          # tasks belong to stages
    assert planner.prompt_engineering.system.endswith("asf/agents/planner/agent.md")

    config = factory_repo / "asf" / "factory.yaml"
    raw = yaml.safe_load(config.read_text())
    raw["agents"] = [{"name": "x"}]
    config.write_text(yaml.safe_dump(raw))
    with pytest.raises(SystemExit, match="does not belong in factory.yaml"):
        factory.load()


def test_an_agent_is_one_file_whose_frontmatter_the_model_never_sees(factory_repo):
    from engine import prompts
    planner = next(a for a in factory.load().agents if a.name == "planner")
    rendered = prompts.render(planner.prompt_engineering.system, {})
    assert rendered.startswith("# Planner") and "writes:" not in rendered

    agent = factory_repo / "asf" / "agents" / "half"
    agent.mkdir()
    (agent / "agent.md").write_text("# Half\n\nNo frontmatter here.\n")
    with pytest.raises(SystemExit, match="has no frontmatter"):
        factory.load()
    (agent / "agent.md").write_text("---\npurpose: x\n---\n\n\n")
    with pytest.raises(SystemExit, match="says nothing below the frontmatter"):
        factory.load()
    (agent / "agent.md").write_text("---\npurpose: x\nname: other\n---\n\nWho.\n")
    with pytest.raises(SystemExit, match="the name is the directory's"):
        factory.load()
    (agent / "agent.md").write_text("---\npurpose: x\n\nWho.\n")
    with pytest.raises(SystemExit, match="never closes it"):
        factory.load()

    # A harness-specific identity is prose only; the boundary stays in agent.md.
    (agent / "agent.md").write_text("---\npurpose: x\nwrites: []\n---\n\n# Neutral\n")
    (agent / "agent.fake.md").write_text("# For the fake harness\n")
    half = next(a for a in factory.load().agents if a.name == "half")
    assert half.writes == [] and half.prompt_engineering.system.endswith("agent.fake.md")
    assert prompts.render(half.prompt_engineering.system, {}).startswith("# For the fake")
    (agent / "agent.fake.md").write_text("---\nwrites: [src/]\n---\n# Wider\n")
    with pytest.raises(SystemExit, match="carries frontmatter"):
        factory.load()


def test_a_stage_outside_the_vocabulary_is_refused_with_the_vocabulary(factory_repo):
    write_workflow(factory_repo, "bad", {"description": "x",
                                         "stages": [{"deploy": {}}]})
    message = refused("bad")
    assert "'deploy' is not a stage" in message
    assert "commit, document, implement, integrate, plan, review, scout, verify" in message


def test_an_input_outside_the_three_is_refused(factory_repo):
    write_workflow(factory_repo, "bad", {"description": "x", "input": "mail", "stages": QUICK})
    assert "input" in refused("bad")


def test_an_option_no_stage_takes_is_refused(factory_repo):
    write_workflow(factory_repo, "bad", {"description": "x",
                                         "stages": [{"implement": {"agent": "builder", "loops": 3}}]})
    assert "loops" in refused("bad")


def test_a_verify_with_nothing_to_verify_is_refused(factory_repo):
    write_workflow(factory_repo, "bad", {"description": "x",
                                         "stages": [{"verify": {}}, {"implement": {}}]})
    message = refused("bad")
    assert "verify: needs a BuildOutput" in message and "hands on nothing" in message


def test_review_and_document_need_a_build_to_work_on(factory_repo):
    write_workflow(factory_repo, "bad", {"description": "x",
                                         "stages": [{"plan": {}}, {"review": {}}]})
    assert "review: needs a BuildOutput" in refused("bad")
    write_workflow(factory_repo, "bad2", {"description": "x",
                                          "stages": [{"document": {}}]})
    assert "document: needs a BuildOutput" in refused("bad2")
    write_workflow(factory_repo, "ok", {"description": "x",
                                        "stages": [{"implement": {}}, {"document": {}},
                                                   {"commit": {"of": "document"}},
                                                   {"integrate": {"mode": "none"}}]})
    assert [s.stage.name for s in workflow.load("ok").steps] == ["implement", "document",
                                                                 "commit", "integrate"]
    write_workflow(factory_repo, "bad3", {"description": "x",
                                          "stages": [{"integrate": {"mode": "rebase"}}]})
    assert "mode" in refused("bad3")


def test_a_commit_of_a_stage_that_wrote_no_message_is_refused(factory_repo):
    write_workflow(factory_repo, "bad", {"description": "x",
                                         "stages": [{"implement": {}}, {"commit": {"of": "review"}}]})
    assert "of: 'review' is not a stage before this one" in refused("bad")


def test_an_agent_the_roster_lacks_is_refused(factory_repo):
    write_workflow(factory_repo, "bad", {"description": "x",
                                         "stages": [{"implement": {"agent": "coder"}}]})
    assert "agent 'coder' is neither in the roster nor bound" in refused("bad")


def test_a_task_whose_report_drifted_from_the_type_is_refused(factory_repo):
    drifted = {**BUILD_REPORT, "changed": ["app.py"]}
    del drifted["changed_files"]
    write_workflow(factory_repo, "bad", {"description": "x", "stages": QUICK},
                   tasks={"implement": task_text("Implement", "m", drifted)})
    message = refused("bad")
    assert "['changed']" in message and "BuildOutput has no field" in message


def test_a_task_that_omits_a_placeholder_is_refused(factory_repo):
    text = task_text("Implement", "m", BUILD_REPORT).replace("{{context_handoff_dir}}", "")
    write_workflow(factory_repo, "bad", {"description": "x", "stages": QUICK},
                   tasks={"implement": text})
    assert "does not mention {{context_handoff_dir}}" in refused("bad")


def test_a_binding_may_narrow_writes_but_never_widen_them(factory_repo):
    write_workflow(factory_repo, "narrow", {
        "description": "x",
        "agents": {"planner": {"from": "planner", "writes": ["specs/api/"]}},
        "stages": [{"plan": {"agent": "planner"}}]})
    loaded = workflow.load("narrow")
    assert next(a for a in loaded.cfg.agents if a.name == "planner").writes == ["specs/api/"]

    write_workflow(factory_repo, "wide", {
        "description": "x",
        "agents": {"planner": {"from": "planner", "writes": ["specs/", "src/"]}},
        "stages": [{"plan": {"agent": "planner"}}]})
    assert "writes ['src/'] are not covered" in refused("wide")


def test_a_binding_may_append_to_an_identity_but_never_replace_it(factory_repo):
    write_workflow(factory_repo, "bad", {
        "description": "x",
        "agents": {"builder": {"from": "builder", "system": "agents/other.md"}},
        "stages": QUICK})
    assert "system" in refused("bad") and "extra" in refused("bad").lower()

    write_workflow(factory_repo, "missing", {
        "description": "x",
        "agents": {"builder": {"from": "builder", "system_append": ["agents/nope.md"]}},
        "stages": QUICK})
    assert "system_append agents/nope.md not found" in refused("missing")


def test_an_alias_binds_a_roster_agent_under_a_new_name(factory_repo):
    write_workflow(factory_repo, "aliased", {
        "description": "x",
        "agents": {"fixer": {"from": "builder", "thinking": "high"}},
        "stages": [{"implement": {"agent": "builder"}},
                   {"verify": {"fix": {"agent": "fixer"}}},
                   {"commit": {"of": "implement"}}]})
    loaded = workflow.load("aliased")
    assert loaded.required_agents == ["builder", "fixer"]
    fixer = next(a for a in loaded.cfg.agents if a.name == "fixer")
    assert fixer.thinking == "high" and fixer.harness == "fake"

    write_workflow(factory_repo, "orphan", {
        "description": "x",
        "agents": {"fixer": {"from": "nobody"}},
        "stages": QUICK})
    assert "from 'nobody' is not in the roster" in refused("orphan")


def test_a_stage_s_hitl_option_lands_in_the_gate_policy(factory_repo):
    write_workflow(factory_repo, "gated", {
        "description": "x",
        "stages": [{"plan": {"hitl": True}}, {"implement": {}}, {"commit": {"of": "implement"}}]})
    assert workflow.load("gated").cfg.hitl.gates == {"plan": "on"}
    assert workflow.load("sdlc").cfg.hitl.gates == {}           # no opinion: factory.yaml's


def test_the_stage_output_is_what_the_next_stage_sees(factory_repo):
    loaded = workflow.load("quick")
    assert loaded.steps[1].stage.needs == (BuildOutput,)
    assert loaded.steps[2].stage.output is None                 # commit passes through


def test_a_stage_module_that_breaks_the_contract_is_refused(factory_repo):
    broken = factory_repo / "asf" / "stages" / "half"
    broken.mkdir()
    (broken / "stage.py").write_text("NAME = 'half'\n")
    with pytest.raises(SystemExit, match="does not meet the contract"):
        workflow.load("quick")
