from anthropic_tokenizer_delta import (TOLERANCE, count_body, parse_budgets,
                                       ratio, rebaseline, repair_lines,
                                       same_apart_from_model, swap_model,
                                       verdict, workload_ratio)

BODY = {
    "model": "claude-sonnet-4-6",
    "system": "You are a scientist",
    "messages": [{"role": "user", "content": "Hello, Claude"}],
    "tools": [{"name": "get_weather", "description": "weather",
               "input_schema": {"type": "object", "properties": {}}}],
    "thinking": {"type": "enabled", "budget_tokens": 16000},
    "max_tokens": 1024,
    "temperature": 0.2,
}


def test_a_body_that_drifted_never_produces_a_ratio():
    left = swap_model(count_body(BODY), "claude-sonnet-4-6")
    right = swap_model(count_body(BODY), "claude-opus-5")
    assert same_apart_from_model(left, right)
    # One word of drift in the system prompt looks exactly like a tokenizer
    # delta and is not one.
    drifted = dict(right, system="You are a careful scientist")
    assert not same_apart_from_model(left, drifted)
    state, detail = verdict([{"name": "a.json", "mismatch": True}],
                            "claude-sonnet-4-6", "claude-opus-5")
    assert state == "bodies-differ"
    assert "no ratio was taken" in detail
    assert any("swap only model" in line for line in repair_lines(state, None))


def test_the_workload_ratio_is_token_weighted_and_not_a_mean_of_ratios():
    rows = [{"base_tokens": 40000, "target_tokens": 52000, "ratio": 1.3},
            {"base_tokens": 200, "target_tokens": 400, "ratio": 2.0}]
    # A mean of the two ratios would be 1.65. The bill follows the tokens.
    assert abs(workload_ratio(rows) - (52400 / 40200)) < 1e-9
    assert workload_ratio(rows) < 1.32
    assert workload_ratio([]) is None
    assert workload_ratio([{"base_tokens": 0, "target_tokens": 10}]) is None


def test_two_ids_on_the_same_tokenizer_are_a_non_finding():
    rows = [{"name": "a.json", "base_tokens": 1000, "target_tokens": 1005,
             "ratio": 1.005}]
    state, detail = verdict(rows, "claude-opus-5", "claude-sonnet-5")
    assert state == "counts-agree"
    assert "share a tokenizer" in detail
    assert any("transfer to the other" in line for line in repair_lines(state, 1.005))
    assert abs(1.005 - 1.0) < TOLERANCE


def test_the_delta_is_reported_with_what_it_costs_and_what_it_breaks():
    rows = [{"name": "a.json", "base_tokens": 18204, "target_tokens": 23551,
             "ratio": 1.2937}]
    state, detail = verdict(rows, "claude-sonnet-4-6", "claude-opus-5")
    assert state == "tokenizer-delta"
    assert "claude-opus-5" in detail and "1.294" in detail
    lines = repair_lines(state, workload_ratio(rows))
    assert any("key any stored token count by model" in line for line in lines)
    assert any("29%" in line for line in lines)
    assert any("retrieval quality" in line for line in lines)


def test_counting_bodies_drop_generation_fields_and_keep_the_window():
    counted = count_body(BODY)
    assert "max_tokens" not in counted and "temperature" not in counted
    for kept in ("system", "messages", "tools", "thinking"):
        assert kept in counted
    assert count_body(None) == {}
    assert swap_model(counted, "claude-fable-5")["model"] == "claude-fable-5"
    # swap_model does not mutate what it was handed.
    assert counted["model"] == "claude-sonnet-4-6"


def test_budgets_are_parsed_forgivingly_and_rebaselined_in_order():
    budgets = parse_budgets(["history=120000,chunk=800", "junk", "bad=x",
                             "zero=0"])
    assert budgets == {"history": 120000, "chunk": 800}
    assert rebaseline(budgets, 1.33) == [("chunk", 800, 1064),
                                         ("history", 120000, 159600)]
    assert rebaseline(budgets, None) == []


def test_a_413_is_handed_to_the_byte_note_rather_than_counted():
    rows = [{"name": "big.json", "error": "HTTP 413 Request exceeds the "
                                          "maximum allowed number of bytes."}]
    state, detail = verdict(rows, "claude-sonnet-4-6", "claude-opus-5")
    assert state == "count-failed"
    assert "413" in detail
    assert any("32 MB byte ceiling" in line for line in repair_lines(state, None))
    assert ratio(0, 10) is None and ratio(None, 10) is None
    assert verdict([], "a", "b")[0] == "no-bodies"
