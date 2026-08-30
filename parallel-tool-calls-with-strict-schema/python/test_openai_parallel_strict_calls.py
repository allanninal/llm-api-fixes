from openai_parallel_strict_calls import (classify, duplicate_names,
                                          exposure, function_calls,
                                          parallel_allowed, parse_ids,
                                          repair_lines, strict_tools,
                                          unvalidated_calls)

STRICT_TOOLS = [
    {"type": "function", "name": "lookup_order", "strict": True},
    {"type": "function", "name": "create_ticket", "strict": True},
]


def turn(calls, tools=None, parallel=None):
    body = {"tools": tools if tools is not None else STRICT_TOOLS,
            "output": [{"type": "function_call", "name": n,
                        "call_id": "call_%d" % i}
                       for i, n in enumerate(calls)]}
    if parallel is not None:
        body["parallel_tool_calls"] = parallel
    return body


def test_a_turn_that_fans_out_under_strict_schemas_has_no_guarantee():
    # The note in one assertion. Three calls, strict declared, and
    # parallel_tool_calls never set, which means true.
    body = turn(["lookup_order", "create_ticket", "create_ticket"])
    assert parallel_allowed(body) is True
    assert strict_tools(body) == ["create_ticket", "lookup_order"]
    assert len(function_calls(body)) == 3

    state, detail = classify(body)
    assert state == "strict-void"
    assert "3 function_call item(s) in one turn" in detail
    assert "carry no schema guarantee" in detail
    assert "parallel_tool_calls false" in repair_lines(state)[0]


def test_the_same_configuration_returning_one_call_is_at_risk_not_clean():
    # The pair. Identical request, one call instead of three, and calling this
    # a pass is how a thousand responses with twelve fan-outs read as fine.
    state, detail = classify(turn(["lookup_order"]))
    assert state == "strict-at-risk"
    assert "The configuration is loaded; it did not fire here." in detail

    states = ["strict-void"] * 12 + ["strict-at-risk"] * 988
    # And 400 unrelated turns that never claimed a guarantee, which must not
    # dilute the denominator.
    states += ["no-strict-declared"] * 400
    shape = exposure(states)
    assert shape["at_risk"] == 1000 and shape["void"] == 12
    assert round(shape["rate"], 4) == 0.012

    rows = [{"state": "strict-void", "calls": 3} for _ in range(9)]
    rows += [{"state": "strict-at-risk", "calls": 1} for _ in range(988)]
    assert unvalidated_calls(rows) == 27


def test_turning_parallel_calls_off_restores_the_guarantee():
    state, detail = classify(turn(["lookup_order"], parallel=False))
    assert state == "strict-serialised"
    assert "The guarantee holds." in detail
    assert parallel_allowed({"parallel_tool_calls": False}) is False
    assert parallel_allowed({"parallel_tool_calls": True}) is True
    assert parallel_allowed({}) is True
    assert exposure(["strict-serialised"] * 40)["rate"] is None


def test_the_same_tool_called_twice_keeps_both_call_ids():
    calls = function_calls(turn(["create_ticket", "create_ticket"]))
    assert duplicate_names(calls) == {"create_ticket": 2}
    assert [c["call_id"] for c in calls] == ["call_0", "call_1"]
    assert duplicate_names([{"name": "a"}, {"name": "b"}]) == {}
    assert duplicate_names(None) == {}


def test_a_fan_out_with_no_strict_tools_is_a_different_fault():
    loose = [{"type": "function", "name": "lookup_order"},
             {"type": "function", "name": "create_ticket", "strict": False}]
    state, detail = classify(turn(["lookup_order", "create_ticket"], tools=loose))
    assert state == "fanout-no-strict"
    assert "no tool declares strict" in detail
    assert "different fault" in detail
    assert "Validate tool arguments" in repair_lines(state)[0]
    assert classify(turn([], tools=loose))[0] == "no-strict-declared"
    assert strict_tools({"tools": loose}) == []


def test_strict_is_read_in_both_tool_shapes():
    nested = [{"type": "function",
               "function": {"name": "run_refund", "strict": True}}]
    assert strict_tools({"tools": nested}) == ["run_refund"]
    state, _ = classify({"tools": nested,
                         "output": [{"type": "function_call", "name": "run_refund",
                                     "call_id": "c1"},
                                    {"type": "function_call", "name": "run_refund",
                                     "call_id": "c2"}]})
    assert state == "strict-void"


def test_turns_without_tools_and_junk_do_not_become_findings():
    assert classify({})[0] == "no-tools"
    assert classify(None)[0] == "no-tools"
    assert classify({"tools": [], "output": []})[0] == "no-tools"
    # A message item is not a tool call.
    body = turn([])
    body["output"] = [{"type": "message", "content": []}, None, "nonsense"]
    assert function_calls(body) == []
    assert classify(body)[0] == "strict-at-risk"
    assert unvalidated_calls(None) == 0


def test_response_ids_are_validated_before_they_reach_a_url():
    text = "resp_abc123\n# note\n\nresp_abc123\nresp_def456\n../../etc\n"
    assert parse_ids(text) == ["resp_abc123", "resp_def456"]
    assert parse_ids("resp_bad/../x") == []
    assert parse_ids(None) == []
