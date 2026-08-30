from openai_advisory_schema import (classify, declared_format, loose_tools,
                                    repair_lines, schema_blockers, schema_size,
                                    strict_state)

TIGHT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["invoice_id", "total"],
    "properties": {"invoice_id": {"type": "string"},
                   "total": {"type": "number"}},
}


def response(fmt, tools=None):
    body = {"id": "resp_s", "status": "completed", "model": "gpt-5.1",
            "text": {"format": fmt},
            "output": [{"type": "message",
                        "content": [{"type": "output_text",
                                     "text": '{"invoice_id": "INV-1", "total": 1}'}]}]}
    if tools is not None:
        body["tools"] = tools
    return body


def test_a_schema_without_strict_is_advice_and_the_json_still_parsed():
    # The trap: this response is a 200 carrying perfectly valid JSON of the
    # right shape. Nothing about the output says the contract was optional.
    stored = response({"type": "json_schema", "name": "invoice", "schema": TIGHT})
    assert declared_format(stored)[:3] == ("json_schema", "invoice", None)
    assert strict_state("json_schema", None) == "advisory"

    state, detail = classify(stored)
    assert state == "advisory-schema"
    assert "strict absent" in detail
    assert "wrong shape is a legal outcome" in detail
    # The schema is already eligible, so the repair is one line rather than a
    # rewrite. Saying so is the difference between a useful report and a chore.
    assert "one-line change" in " ".join(repair_lines(stored, state))


def test_strict_false_reads_the_same_as_strict_missing():
    stored = response({"type": "json_schema", "name": "invoice",
                       "strict": False, "schema": TIGHT})
    state, detail = classify(stored)
    assert state == "advisory-schema"
    assert "strict false" in detail


def test_legacy_json_object_mode_is_named_as_its_own_thing():
    stored = response({"type": "json_object"})
    state, detail = classify(stored)
    assert state == "no-schema"
    assert "valid JSON and nothing else" in detail
    assert "json_object to a json_schema" in repair_lines(stored, state)[0]


def test_schema_blockers_names_every_rule_the_subset_requires():
    loose = {
        "type": "object",
        "required": ["invoice_id"],
        "properties": {
            "invoice_id": {"type": "string", "minLength": 3},
            "note": {"type": "string"},
            "lines": {"type": "array",
                      "items": {"type": "object",
                                "additionalProperties": False,
                                "properties": {"sku": {"type": "string"}},
                                "required": ["sku"]}},
        },
    }
    found = " | ".join(schema_blockers(loose))
    assert "$: needs additionalProperties: false" in found
    assert "missing lines, note" in found
    assert "minLength are silently unenforced" in found
    # The nested object below the array already obeys the rules, so it must
    # not be reported. A checker that flags everything gets muted.
    assert "$.lines[]" not in found
    assert schema_blockers(TIGHT) == []


def test_a_root_that_is_not_a_plain_object_cannot_be_strict_at_all():
    assert "root may not be anyOf" in schema_blockers(
        {"anyOf": [TIGHT, {"type": "object"}]})[0]
    assert "root type must be object, not array" in schema_blockers(
        {"type": "array", "items": TIGHT})[0]
    assert "not a schema object" in schema_blockers(None)[0]


def test_depth_beyond_five_levels_is_reported_and_the_walk_stops():
    schema = {"type": "object", "additionalProperties": False,
              "required": ["a"], "properties": {"a": {"type": "string"}}}
    for _ in range(6):
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["child"], "properties": {"child": schema}}
    found = schema_blockers(schema)
    assert any("past the limit of 5" in f for f in found)
    assert schema_size(schema)["depth"] > 5


def test_a_strict_format_beside_a_loose_tool_is_still_a_gap():
    tools = [{"type": "function", "name": "charge", "parameters": TIGHT,
              "strict": True},
             {"type": "function", "name": "refund", "parameters": TIGHT}]
    stored = response({"type": "json_schema", "name": "invoice",
                       "strict": True, "schema": TIGHT}, tools=tools)
    assert loose_tools(stored) == ["refund"]
    state, detail = classify(stored)
    assert state == "advisory-tools"
    assert "refund" in detail
    assert "every tool as well as on the text format" in repair_lines(stored, state)[0]


def test_the_chat_completions_shape_and_the_clean_cases():
    legacy = {"response_format": {"type": "json_schema",
                                  "json_schema": {"name": "invoice",
                                                  "strict": True,
                                                  "schema": TIGHT}}}
    assert declared_format(legacy)[:3] == ("json_schema", "invoice", True)
    assert classify(legacy)[0] == "enforced"
    assert classify({})[0] == "free-text"
    assert classify(None)[0] == "free-text"
    assert repair_lines({}, "free-text") == []
    assert loose_tools({}) == []
    assert schema_size({}) == {"properties": 0, "depth": 1, "enum": 0}
