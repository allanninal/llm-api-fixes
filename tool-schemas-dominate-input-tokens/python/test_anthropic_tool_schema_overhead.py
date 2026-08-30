from anthropic_tool_schema_overhead import (choice_kind, classify, countable,
                                            defer_candidates, fixed_overhead,
                                            monthly_cost, overhead,
                                            overhead_share,
                                            system_prompt_tokens, tool_names,
                                            window_share, without_tool,
                                            without_tools)

BODY = {
    "model": "claude-opus-5",
    "max_tokens": 1024,
    "temperature": 0,
    "system": "You are a support agent.",
    "tool_choice": {"type": "auto"},
    "messages": [{"role": "user", "content": "where is my order"}],
    "tools": [
        {"name": "search_knowledge_base", "input_schema": {"type": "object"}},
        {"name": "create_ticket", "input_schema": {"type": "object"}},
        {"name": "lookup_order", "input_schema": {"type": "object"}},
    ],
}


def test_the_tools_block_is_most_of_what_you_pay_for():
    # The note in one assertion. Two free counts, one subtraction.
    total, base = 12388, 888
    assert overhead(total, base) == 11500
    assert round(overhead_share(total, base), 4) == 0.9283

    state, detail = classify(total, base)
    assert state == "schema-dominates"
    assert "11500 of 12388 input token(s) are the tools block (93%)" in detail
    assert "888 token(s) of system and messages, a ratio of 13.0 to 1" in detail

    # 11500 tokens on 10000 calls a day for 30 days at $3 per million.
    assert monthly_cost(11500, 10000, 3.0) == 10350.0


def test_the_ablation_deltas_do_not_add_up_to_the_whole():
    # The trap. Removing one tool never removes the tool-use system prompt, so
    # the per-tool sum is the schema weight and the residual is the fixed
    # charge. A script that printed the sum as the total would be wrong by 286
    # tokens on every call and would look right.
    per_tool = [{"name": "search_knowledge_base", "tokens": 6200},
                {"name": "create_ticket", "tokens": 3100},
                {"name": "lookup_order", "tokens": 1914}]
    residual, measured = fixed_overhead(11500, per_tool)
    assert measured == 11214
    assert residual == 286
    assert residual == system_prompt_tokens("claude-opus-5", "auto")
    assert fixed_overhead(0, per_tool) == (0, 11214)


def test_the_system_prompt_table_matches_on_longest_prefix():
    assert system_prompt_tokens("claude-opus-5") == 286
    assert system_prompt_tokens("claude-opus-5", "any") == 406
    assert system_prompt_tokens("claude-sonnet-5") == 354
    # The one a careless substring match gets wrong: 4-5 is not 5.
    assert system_prompt_tokens("claude-opus-4-5") == 496
    assert system_prompt_tokens("claude-haiku-4-5-20251001") == 496
    assert system_prompt_tokens("claude-opus-4-7", "any") == 804
    # Unlisted returns nothing rather than a neighbour's number.
    assert system_prompt_tokens("claude-fable-5") is None
    assert system_prompt_tokens("") is None
    assert system_prompt_tokens(None) is None


def test_removing_the_tools_removes_the_tool_choice_with_them():
    stripped = without_tools(BODY)
    assert "tools" not in stripped and "tool_choice" not in stripped
    assert stripped["system"] == BODY["system"]
    assert stripped["messages"] == BODY["messages"]
    # And the original is untouched, or the second count measures the first.
    assert len(BODY["tools"]) == 3 and "tool_choice" in BODY

    one_out = without_tool(BODY, "create_ticket")
    assert tool_names(one_out) == ["search_knowledge_base", "lookup_order"]
    assert one_out["tool_choice"] == BODY["tool_choice"]
    # Removing the last tool has to take tool_choice with it as well.
    bare = without_tool({"tools": [{"name": "only"}], "tool_choice": "any"}, "only")
    assert "tools" not in bare and "tool_choice" not in bare


def test_the_deferral_picker_can_never_return_every_tool():
    rows = [{"name": "a", "tokens": 900}, {"name": "b", "tokens": 400},
            {"name": "c", "tokens": 100}]
    picked = defer_candidates(rows)
    assert picked == ["b", "c"]
    assert len(picked) < len(rows)
    # Naming every tool hot leaves nothing to defer, which is also fine.
    assert defer_candidates(rows, hot=["a", "b", "c"]) == []
    assert defer_candidates(rows, hot=["a"]) == ["b", "c"]
    assert defer_candidates([{"name": "only", "tokens": 10}]) == []
    assert defer_candidates([]) == []


def test_the_counting_body_keeps_what_is_being_measured():
    body = countable(BODY)
    assert "max_tokens" not in body and "temperature" not in body
    assert body["tools"] == BODY["tools"]
    assert body["model"] == "claude-opus-5"
    assert countable(None) == {}
    assert choice_kind(BODY) == "auto"
    assert choice_kind({"tool_choice": {"type": "tool", "name": "x"}}) == "any"
    assert choice_kind({"tool_choice": "any"}) == "any"
    assert choice_kind({}) == "auto"


def test_the_states_are_bounded_and_a_missing_number_stays_missing():
    assert classify(1000, 900)[0] == "schema-modest"
    assert classify(1000, 700)[0] == "schema-heavy"
    assert classify(1000, 500)[0] == "schema-dominates"
    assert classify(1000, 1000)[0] == "no-tools"
    assert classify(0, 0)[0] == "nothing-counted"
    assert overhead_share(0, 0) is None
    assert overhead(500, 900) == 0
    assert monthly_cost(11500, 0, 3.0) is None
    assert monthly_cost(11500, 10, "free") is None
    assert window_share(12388, 200000) == 0.06194
    assert window_share(12388, 0) is None
