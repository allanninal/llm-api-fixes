from openai_project_model_policy_audit import (classify, fold_models,
                                               policy_ids, policy_state,
                                               repair_lines, unrestricted,
                                               unused_allowed, unused_tools)

USED = {"gpt-4.1-mini": 41208}


def bucket(*results):
    return {"object": "bucket", "start_time": 0, "end_time": 86400,
            "results": list(results)}


def test_an_absent_policy_and_an_empty_deny_list_are_two_findings():
    # The note. Identical reachability, identical usage, different repairs,
    # because one of these two looks configured in the console and one does not.
    absent_state, absent_detail = classify(None, USED)
    empty_state, empty_detail = classify({"mode": "deny_list", "model_ids": []}, USED)

    assert unrestricted(None) is True
    assert unrestricted({"mode": "deny_list", "model_ids": []}) is True
    assert absent_state == "no-policy"
    assert empty_state == "deny-list-empty"
    assert "looks configured" in empty_detail
    assert "reachable from this project" in absent_detail

    absent_lines = repair_lines(absent_state, "proj_demo", USED)
    empty_lines = repair_lines(empty_state, "proj_batch", USED)
    assert any("does not inherit" in line for line in absent_lines)
    assert any("did not finish it" in line for line in empty_lines)
    assert absent_lines != empty_lines


def test_a_deny_list_with_entries_is_restrictive_today_and_open_tomorrow():
    policy = {"mode": "deny_list", "model_ids": ["gpt-4.1"]}
    assert unrestricted(policy) is False
    state, detail = classify(policy, USED)
    assert state == "deny-list-fails-open"
    assert "released tomorrow" in detail
    assert any("does not exist yet" in line
               for line in repair_lines(state, "proj_x", USED))
    # Subtracting usage from a deny list would be nonsense, so it is not done.
    assert unused_allowed(policy, USED) == []


def test_an_allow_list_wider_than_use_names_only_what_it_measured():
    policy = {"mode": "allow_list",
              "model_ids": ["gpt-4.1-mini", "gpt-4.1", "o3", "gpt-4.1-nano"]}
    state, detail = classify(policy, USED, days=30)
    assert state == "allow-list-wider-than-use"
    assert "names 4 model(s); 1 served any request" in detail
    assert unused_allowed(policy, USED) == ["gpt-4.1", "gpt-4.1-nano", "o3"]
    # Exactly the observed set, and nothing the project never called.
    lines = repair_lines(state, "proj_web", USED)
    assert any("'model_ids': ['gpt-4.1-mini']" in line
               or '"model_ids": [\'gpt-4.1-mini\']' in line
               or "['gpt-4.1-mini']" in line for line in lines)
    assert not any("o3" in line for line in lines)
    # An allow list matching use exactly is not a finding.
    tight = {"mode": "allow_list", "model_ids": ["gpt-4.1-mini"]}
    assert classify(tight, USED)[0] == "restricted"
    assert repair_lines("restricted", "proj_web", USED) == []


def test_the_policy_shape_reader_handles_every_degenerate_case():
    assert policy_state(None) == "absent"
    assert policy_state({"mode": "allow_list", "model_ids": []}) == "allow-empty"
    assert policy_state({"mode": "allow_list", "model_ids": ["  "]}) == "allow-empty"
    assert policy_state({"mode": "ALLOW_LIST", "model_ids": ["a"]}) == "allow-list"
    assert policy_state({"mode": "something_new"}) == "unreadable"
    assert policy_ids({"model_ids": ["a", "", None, " b "]}) == ["a", "b"]
    state, detail = classify({"mode": "allow_list", "model_ids": []}, USED)
    assert state == "allow-list-empty"
    assert "permits nothing" in detail
    assert classify({"mode": "?"}, USED)[0] == "policy-unreadable"


def test_a_tool_with_no_usage_endpoint_is_uncountable_not_unused():
    perms = {"code_interpreter": {"enabled": False},
             "file_search": {"enabled": True},
             "image_generation": {"enabled": True},
             "mcp": {"enabled": True},
             "web_search": {"enabled": True}}
    counts = {"web_search": 4120, "file_search": 0, "image_generation": 0}
    found = dict(unused_tools(perms, counts))
    assert "web_search" not in found          # used, so not reported
    assert "code_interpreter" not in found    # disabled, so not reported
    assert "file_search_calls reports nothing" in found["file_search"]
    assert found["mcp"] == "enabled, and no usage endpoint counts it"
    assert unused_tools(None, None) == []
    assert unused_tools({"web_search": "not a block"}, {}) == []


def test_the_report_never_recommends_a_model_the_project_did_not_call():
    # This note owns the policy object. Which model suits the workload belongs
    # to a different note, and no repair line here may stray into it.
    for state in ("no-policy", "deny-list-empty", "deny-list-fails-open",
                  "allow-list-wider-than-use"):
        for line in repair_lines(state, "proj_a", USED):
            assert "cheaper" not in line and "mini" not in line.replace(
                "gpt-4.1-mini", "")
    empty = repair_lines("no-policy", "proj_idle", {})
    assert any("no observed set" in line for line in empty)
    used = fold_models([bucket({"project_id": "p", "model": "m",
                                "num_model_requests": 4}),
                        bucket({"project_id": "p", "model": "m",
                                "num_model_requests": 0})])
    assert used == {"p": {"m": 4}}
