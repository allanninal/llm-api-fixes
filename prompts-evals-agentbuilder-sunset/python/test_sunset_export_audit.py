from sunset_export_audit import (AGENT_BUILDER, SHUTDOWN, days_left,
                                 export_command, export_plan, prompt_id_state,
                                 repair_lines, surface_reach)

TODAY = "2026-08-31"


def test_a_surface_with_no_api_is_never_promoted_by_a_stray_status():
    # There is no path a 200 could have come from, so one must not make the
    # report look complete. This is the whole reason the row exists.
    for status in (None, 200, 404, 401):
        state, detail = surface_reach(AGENT_BUILDER, status)
        assert state == "no-api-surface"
        assert "no documented REST endpoints" in detail
    assert any("open Agent Builder" in line
               for line in repair_lines("no-api-surface"))


def test_a_404_on_the_prompts_path_means_no_listing_and_not_gone():
    # Those imply different next steps and only one is supported by the
    # evidence: the API reference documents no listing for reusable prompts.
    state, detail = surface_reach("prompts", 404)
    assert state == "no-list-endpoint"
    assert "your own call sites" in detail
    assert "gone" not in detail
    lines = repair_lines(state)
    assert any("grep of your own tree" in line for line in lines)
    assert any("impossible before the export" in line for line in lines)


def test_the_plan_puts_a_person_against_what_no_script_can_reach():
    plan = export_plan([("evals", "enumerable"),
                        ("prompts", "no-list-endpoint"),
                        (AGENT_BUILDER, "no-api-surface"),
                        ("something", "credentials")])
    owners = {name: owner for name, owner, _ in plan}
    assert owners["evals"] == "a script"
    assert owners["prompts"] == "a script, by id"
    assert owners[AGENT_BUILDER] == "a person"
    assert owners["something"].startswith("a person, until")
    assert len(plan) == 4


def test_an_id_that_is_not_a_prompt_id_is_caught_without_a_request():
    state, detail = prompt_id_state("promptx", None)
    assert state == "not-a-prompt-id"
    assert "start pmpt_" in detail
    assert prompt_id_state("", None)[0] == "malformed"
    assert prompt_id_state(None, 200)[0] == "malformed"
    # A real id with no probe is honestly reported as not probed, which is a
    # different thing from unreadable.
    assert prompt_id_state("pmpt_a1b2", None)[0] == "not-probed"


def test_a_declared_id_is_graded_by_what_answered_for_it():
    assert prompt_id_state("pmpt_a1b2", 200)[0] == "readable"
    state, detail = prompt_id_state("  pmpt_c3d4  ", 404)
    assert state == "not-readable"
    assert "out of the dashboard" in detail
    assert prompt_id_state("pmpt_c3d4", 401)[0] == "credentials"
    assert prompt_id_state("pmpt_c3d4", 500)[0] == "refused"


def test_the_export_command_is_a_read():
    line = export_command("evals")
    assert line.startswith("curl -s ")
    assert "/v1/evals?limit=100" in line
    assert "$OPENAI_API_KEY" in line
    assert "-X" not in line
    assert export_command("prompt", "pmpt_a1b2").endswith("export/pmpt_a1b2.json")
    assert export_command("agent-builder") == ""


def test_the_date_is_the_export_deadline_and_the_arithmetic_says_so():
    assert days_left(TODAY) == 91
    assert days_left("2026-11-30") == 0
    assert days_left("2026-12-05") == -5
    assert SHUTDOWN == "2026-11-30"
