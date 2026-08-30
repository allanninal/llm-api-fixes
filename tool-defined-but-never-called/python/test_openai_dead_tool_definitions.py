from openai_dead_tool_definitions import (choice_mode, classify, coverage,
                                          crowding, dead_weight,
                                          declared_tools, fold, orphan_calls,
                                          parse_ids, tool_name)

TOOLS = [
    {"type": "function", "name": "lookup_order", "description": "x" * 200},
    {"type": "function", "name": "cancel_order", "description": "x" * 200},
    {"type": "function", "name": "lookup_invoice", "description": "x" * 200},
    {"type": "function", "name": "escalate_to_human", "description": "x" * 1000},
]


def turn(calls, choice=None, tools=None):
    body = {"tools": tools if tools is not None else TOOLS,
            "output": [{"type": "function_call", "name": n, "call_id": "call_1"}
                       for n in calls]}
    if choice is not None:
        body["tool_choice"] = choice
    return body


def test_a_tool_declared_on_every_turn_and_never_chosen_is_dead_weight():
    # The note in one assertion. Four hundred turns, four tools, one of them
    # absent from every output array.
    sample = [turn(["lookup_order"]) for _ in range(300)]
    sample += [turn(["cancel_order"]) for _ in range(98)]
    sample += [turn(["lookup_invoice"]) for _ in range(2)]
    corpus = fold(sample)
    assert corpus["sampled"] == 400 and corpus["with_tools"] == 400

    rows = {r["name"]: r for r in coverage(corpus)}
    assert rows["escalate_to_human"]["offered"] == 400
    assert rows["escalate_to_human"]["calls"] == 0

    state, detail = classify(rows["escalate_to_human"])
    assert state == "never-called"
    assert "offered in 400 of 400 turn(s), called 0 time(s)" in detail
    assert classify(rows["lookup_order"])[0] == "called"
    assert classify(rows["lookup_invoice"])[0] == "rarely-called"


def test_a_tool_tool_choice_never_offered_is_a_different_finding():
    # Same tool, same zero calls, and not this note: the model never had the
    # chance to decline it, so its description is not the problem.
    sample = [turn(["lookup_order"], choice={"type": "function",
                                             "name": "lookup_order"})
              for _ in range(400)]
    rows = {r["name"]: r for r in coverage(fold(sample))}
    state, detail = classify(rows["escalate_to_human"])
    assert state == "never-offered"
    assert "free to be chosen in 0 of them" in detail
    # And the named tool itself was on the table every time.
    assert rows["lookup_order"]["offered"] == 400
    assert classify(rows["lookup_order"])[0] == "called"


def test_tool_choice_none_is_not_evidence_about_anything():
    sample = [turn([], choice="none") for _ in range(400)]
    rows = {r["name"]: r for r in coverage(fold(sample))}
    assert rows["lookup_order"]["turns"] == 400
    assert rows["lookup_order"]["offered"] == 0
    assert classify(rows["lookup_order"])[0] == "never-offered"
    assert choice_mode({"tool_choice": "none"}) == "blocked"
    assert choice_mode({}) == "free"
    assert choice_mode({"tool_choice": "auto"}) == "free"
    assert choice_mode({"tool_choice": "required"}) == "free"


def test_both_tool_shapes_are_read():
    nested = [{"type": "function", "function": {"name": "run_refund"}}]
    assert tool_name(nested[0]) == "run_refund"
    assert tool_name({"type": "function", "name": "flat"}) == "flat"
    assert tool_name({"type": "web_search"}) is None
    assert tool_name(None) is None
    # A hosted tool carries no name and is not a definition you can prune.
    assert declared_tools({"tools": [{"type": "web_search"}]}) == {}
    assert set(declared_tools({"tools": nested})) == {"run_refund"}


def test_a_small_sample_is_not_a_verdict():
    rows = {r["name"]: r for r in coverage(fold([turn([]) for _ in range(11)]))}
    state, detail = classify(rows["lookup_order"])
    assert state == "too-small-a-sample"
    assert "under the floor of 50" in detail
    assert classify(rows["lookup_order"], min_offered=5)[0] == "never-called"


def test_the_dead_weight_share_is_characters_and_stays_characters():
    sample = [turn(["lookup_order", "cancel_order", "lookup_invoice"])
              for _ in range(400)]
    rows = coverage(fold(sample))
    share = dead_weight(rows)
    # escalate_to_human carries the 1000 character description; the other three
    # carry 200 each, so the dead share is well over half.
    assert 0.5 < share < 0.75
    assert dead_weight([]) is None
    assert dead_weight([{"name": "a", "chars": 0, "turns": 1, "offered": 1,
                         "calls": 0}]) is None


def test_a_crowded_turn_is_its_own_finding():
    wide = [{"type": "function", "name": "tool_%d" % i} for i in range(26)]
    corpus = fold([turn([], tools=wide) for _ in range(60)])
    state, detail = crowding(corpus["widest_turn"])
    assert state == "crowded"
    assert "offered 26 tools" in detail
    assert crowding(20)[0] == "within-guidance"
    assert crowding(0)[0] == "no-tools"


def test_a_mixed_sample_is_reported_rather_than_silently_subtracted():
    corpus = fold([turn(["from_another_config"])])
    assert orphan_calls(corpus) == ["from_another_config"]
    assert orphan_calls(fold([turn(["lookup_order"])])) == []
    assert fold([]) == fold(None)
    assert coverage(fold(None)) == []


def test_response_ids_are_validated_before_they_reach_a_url():
    text = "resp_abc123\n# a comment\n\nresp_abc123\nresp_def456\n../../etc\n"
    assert parse_ids(text) == ["resp_abc123", "resp_def456"]
    assert parse_ids("resp_bad/../x") == []
    assert parse_ids(None) == []
