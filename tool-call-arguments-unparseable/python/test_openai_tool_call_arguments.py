from openai_tool_call_arguments import (classify, declared_tools,
                                        function_calls, parse_arguments,
                                        repair_lines, schema_violations,
                                        was_truncated)

CHARGE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["account_id", "amount_cents", "currency"],
    "properties": {
        "account_id": {"type": "string"},
        "amount_cents": {"type": "integer"},
        "currency": {"type": "string", "enum": ["usd", "eur"]},
    },
}


def response(arguments, *, name="charge", strict=True, status="completed"):
    return {"id": "resp_t", "status": status,
            "tools": [{"type": "function", "name": "charge",
                       "parameters": CHARGE, "strict": strict}],
            "output": [{"type": "function_call", "name": name,
                        "call_id": "call_1", "arguments": arguments}]}


def test_arguments_that_parse_and_still_break_the_contract():
    # The centre of the note. json.loads is perfectly happy; the handler is
    # not, and no amount of care around the parse would have caught it.
    stored = response('{"account_id": "acct_9", "amount_cents": "1200", '
                      '"currency": "gbp", "idempotency_key": "k1"}')
    call = function_calls(stored)[0]
    value, error = parse_arguments(call["arguments"])
    assert error is None and isinstance(value, dict)

    state, detail = classify(call, declared_tools(stored))
    assert state == "arguments-violate-schema"
    assert "amount_cents: expected integer, got str" in detail
    assert "currency: 'gbp' is not one of the 2 declared value(s)" in detail
    assert "idempotency_key: not declared" in detail
    assert "feed the validation error back to the model" in repair_lines(state)[0]


def test_a_missing_required_argument_is_found_before_the_handler_is_called():
    stored = response('{"account_id": "acct_9", "currency": "usd"}')
    state, detail = classify(function_calls(stored)[0], declared_tools(stored))
    assert state == "arguments-violate-schema"
    assert "arguments.amount_cents: required and missing" in detail


def test_a_cut_argument_string_belongs_to_the_truncation_note():
    stored = response('{"account_id": "acct_9", "amount_cent',
                      status="incomplete")
    stored["incomplete_details"] = {"reason": "max_output_tokens"}
    assert was_truncated(stored) is True
    state, detail = classify(function_calls(stored)[0], declared_tools(stored),
                             was_truncated(stored))
    assert state == "arguments-truncated"
    assert "cut mid-write rather than written wrongly" in detail
    assert "Not a schema problem" in repair_lines(state)[0]


def test_a_broken_string_on_a_completed_response_is_the_models_own_work():
    stored = response('{{"account_id": "acct_9"}}')
    assert was_truncated(stored) is False
    state, detail = classify(function_calls(stored)[0], declared_tools(stored))
    assert state == "arguments-unparseable"
    assert "nothing was constraining the grammar" in detail


def test_an_unknown_tool_name_is_a_lookup_error_not_a_parse_error():
    stored = response('{"account_id": "acct_9"}', name="charge_v2")
    state, detail = classify(function_calls(stored)[0], declared_tools(stored))
    assert state == "unknown-tool"
    assert "indexes a handler map by name raises here" in detail
    assert "renamed on one side only" in repair_lines(state)[1]


def test_a_valid_call_is_dispatchable_and_an_unstrict_one_is_flagged_anyway():
    good = '{"account_id": "acct_9", "amount_cents": 1200, "currency": "usd"}'
    stored = response(good)
    assert classify(function_calls(stored)[0], declared_tools(stored))[0] == "dispatchable"

    loose = response(good, strict=False)
    state, detail = classify(function_calls(loose)[0], declared_tools(loose))
    assert state == "dispatchable-unconstrained"
    assert "nothing guaranteed that it would" in detail


def test_the_chat_completions_shape_and_the_empty_argument_string():
    legacy = {"choices": [{"finish_reason": "tool_calls", "message": {
        "tool_calls": [{"id": "call_9", "type": "function",
                        "function": {"name": "ping", "arguments": ""}}]}}],
        "tools": [{"type": "function",
                   "function": {"name": "ping", "strict": True,
                                "parameters": {"type": "object",
                                               "additionalProperties": False,
                                               "properties": {},
                                               "required": []}}}]}
    call = function_calls(legacy)[0]
    assert call["name"] == "ping" and call["call_id"] == "call_9"
    # A tool that takes nothing is legally called with an empty string, and a
    # bare json.loads raises on it.
    assert parse_arguments("") == ({}, None)
    assert classify(call, declared_tools(legacy))[0] == "dispatchable"


def test_the_walker_and_the_readers_survive_junk():
    assert parse_arguments(None)[1] == "the arguments field is absent"
    assert parse_arguments("[1, 2]")[1] == "arguments parsed to list, not an object"
    assert schema_violations({"a": 1}, None) == []
    assert schema_violations({"a": 1}, {}) == []
    assert schema_violations(True, {"type": "integer"}) == [
        "arguments: expected integer, got bool"]
    assert schema_violations({"rows": [{"sku": 1}]}, {
        "type": "object", "properties": {"rows": {
            "type": "array", "items": {"type": "object",
                                       "properties": {"sku": {"type": "string"}}}}}}) == [
        "arguments.rows[0].sku: expected string, got int"]
    assert function_calls(None) == []
    assert declared_tools(None) == {}
    assert was_truncated(None) is False
