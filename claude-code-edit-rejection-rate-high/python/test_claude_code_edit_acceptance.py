import datetime as dt

from claude_code_edit_acceptance import (acceptance, actions_of, actor_name,
                                         day_strings, fold, mask, repair_lines,
                                         totals, verdict, worst_tool)


def record(email, tools, commits=0, cents="0", prs=0):
    return {"date": "2026-08-30",
            "actor": {"type": "user_actor", "email_address": email},
            "core_metrics": {"num_sessions": 6,
                             "commits_by_claude_code": commits,
                             "pull_requests_by_claude_code": prs,
                             "lines_of_code": {"added": 400, "removed": 90}},
            "tool_actions": tools,
            "model_breakdown": [{"model": "claude-opus-5",
                                 "tokens": {"input": 1, "output": 1},
                                 "estimated_cost": {"currency": "USD",
                                                    "amount": cents}}]}


def page(records):
    return {"data": records, "has_more": False}


def test_a_majority_of_generated_diffs_being_thrown_away_is_the_finding():
    # The note in one assertion. Every one of these 256 rejections was fully
    # generated and fully billed before anybody read it.
    rows = fold([page([record("busy@example.com", {
        "edit_tool": {"accepted": 120, "rejected": 80},
        "multi_edit_tool": {"accepted": 36, "rejected": 136},
        "write_tool": {"accepted": 0, "rejected": 40},
    }, commits=4, cents="31040")])])
    row = rows["busy@example.com"]
    assert totals(row) == (156, 256)

    state, detail = verdict(row)
    assert state == "rejected-more-than-kept"
    assert "38% accepted over 412 proposal(s)" in detail
    # The average hides which tool is the problem, so the worst is named.
    assert "worst tool write_tool at 0%" in detail
    assert worst_tool(row)[0] == "write_tool"
    assert any("CLAUDE.md" in line for line in repair_lines(state, row))


def test_a_bad_afternoon_is_not_a_pattern():
    rows = fold([page([record("quiet@example.com", {
        "edit_tool": {"accepted": 2, "rejected": 7}})])])
    state, detail = verdict(rows["quiet@example.com"])
    assert state == "too-few-proposals"
    assert "under the floor of 20" in detail
    assert repair_lines(state, rows["quiet@example.com"]) == []


def test_the_commits_travel_with_the_rate_so_it_is_never_read_alone():
    landing = fold([page([record("lands@example.com", {
        "edit_tool": {"accepted": 90, "rejected": 160}}, commits=26)])])
    row = landing["lands@example.com"]
    state, _ = verdict(row)
    assert state == "rejected-more-than-kept"
    assert any("26 commit(s)" in line for line in repair_lines(state, row))

    empty = fold([page([record("none@example.com", {
        "edit_tool": {"accepted": 90, "rejected": 160}}, commits=0)])])
    lines = repair_lines("rejected-more-than-kept", empty["none@example.com"])
    assert any("no commits landed" in line for line in lines)


def test_an_actor_who_proposed_nothing_has_no_rate_rather_than_zero():
    assert acceptance({"accepted": 0, "rejected": 0}) is None
    assert acceptance({}) is None
    assert acceptance({"accepted": 3, "rejected": 1}) == 0.75
    assert worst_tool({"tools": {}}) is None
    # Under the per-tool floor there is no worst tool to name.
    assert worst_tool({"tools": {"edit_tool": {"accepted": 1, "rejected": 2}}}) is None


def test_a_tool_nobody_used_is_absent_and_not_a_zero():
    actions = actions_of({"tool_actions": {
        "edit_tool": {"accepted": 4, "rejected": 1},
        "write_tool": {"accepted": 0, "rejected": 0},
        "bash_tool": {"accepted": 99, "rejected": 99}}})
    assert list(actions) == ["edit_tool"]
    assert actions_of({}) == {}
    assert actions_of(None) == {}
    assert actions_of({"tool_actions": {"edit_tool": {"accepted": "x",
                                                      "rejected": 3}}}) == \
        {"edit_tool": {"accepted": 0, "rejected": 3}}


def test_counts_accumulate_across_days_and_across_actor_shapes():
    day = [record("a@example.com", {"edit_tool": {"accepted": 10, "rejected": 5}},
                  commits=1, cents="500"),
           {"actor": {"type": "api_actor", "api_key_name": "ci-runner"},
            "core_metrics": {"num_sessions": 1},
            "tool_actions": {"edit_tool": {"accepted": 30, "rejected": 2}},
            "model_breakdown": []}]
    rows = fold([page(day), page(day)])
    assert totals(rows["a@example.com"]) == (20, 10)
    assert rows["a@example.com"]["commits"] == 2
    assert rows["a@example.com"]["cents"] == 1000
    assert rows["a@example.com"]["added"] == 800
    assert totals(rows["ci-runner"]) == (60, 4)
    assert verdict(rows["ci-runner"])[0] == "healthy"


def test_actors_are_resolved_and_masked_before_being_printed():
    assert actor_name({"actor": {"email_address": "a@example.com"}}) == "a@example.com"
    assert actor_name({"actor": {"api_key_name": "ci"}}) == "ci"
    assert actor_name({}) == "unattributed"
    assert mask("someone@example.com") == "s***@example.com"
    assert mask("ci-runner") == "ci-runner"
    assert mask(None) == "unattributed"


def test_the_thin_band_sits_between_kept_and_healthy():
    row = {"tools": {"edit_tool": {"accepted": 61, "rejected": 39}}, "commits": 0}
    assert verdict(row)[0] == "low-acceptance"
    row = {"tools": {"edit_tool": {"accepted": 88, "rejected": 12}}, "commits": 0}
    assert verdict(row)[0] == "healthy"
    assert day_strings(2, dt.date(2026, 3, 1)) == ["2026-02-28", "2026-02-27"]
    assert fold([]) == {} and fold(None) == {}
