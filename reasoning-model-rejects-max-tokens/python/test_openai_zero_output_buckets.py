from openai_zero_output_buckets import (classify, fold, is_reasoning_model,
                                        model_verdict, repair_lines,
                                        silent_share)


def bucket(project, model, requests_made, input_tokens, output_tokens):
    return {"results": [{"project_id": project, "model": model,
                         "num_model_requests": requests_made,
                         "input_tokens": input_tokens,
                         "output_tokens": output_tokens}]}


def test_requests_with_no_tokens_either_side_is_a_rejected_body():
    # The note in one assertion. Every call counted, nothing read, nothing
    # written: the body never got past validation.
    rows = fold([bucket("proj_api", "gpt-5.1", 500, 0, 0) for _ in range(24)])
    row = rows[("proj_api", "gpt-5.1")]
    assert row["requests"] == 12000
    assert row["buckets"] == 24 and row["silent_buckets"] == 24
    assert silent_share(row) == 1.0

    state, detail = classify("gpt-5.1", row)
    assert state == "parameter-rejected"
    assert "0 input token(s) and 0 output token(s)" in detail
    assert "max_completion_tokens" in repair_lines("gpt-5.1")[0]
    assert "max_output_tokens" in repair_lines("gpt-5.1")[1]


def test_input_read_and_nothing_generated_is_a_different_finding():
    # Same request count, same zero output, and not this note: the prompt
    # reached the model, so the body was accepted and generation was blocked.
    rows = fold([bucket("proj_api", "gpt-5.1", 500, 900000, 0) for _ in range(24)])
    state, detail = classify("gpt-5.1", rows[("proj_api", "gpt-5.1")])
    assert state == "generation-blocked"
    assert "verification" in detail


def test_a_partial_rollout_is_not_rounded_up_to_a_total_outage():
    silent = [bucket("proj_api", "o3-mini", 100, 0, 0) for _ in range(6)]
    healthy = [bucket("proj_api", "o3-mini", 100, 200000, 40000) for _ in range(18)]
    row = fold(silent + healthy)[("proj_api", "o3-mini")]
    assert silent_share(row) == 0.25
    state, detail = classify("o3-mini", row)
    assert state == "partial-rejection"
    assert "25%" in detail


def test_the_reasoning_families_are_matched_as_whole_prefixes():
    for model in ("o1", "o3-mini", "o4-mini", "gpt-5", "gpt-5.1-mini",
                  "gpt-5-2026-01-15"):
        assert is_reasoning_model(model) is True
    # gpt-4o is the one a careless substring match gets wrong.
    for model in ("gpt-4o", "gpt-4o-mini", "gpt-4.1", "claude-sonnet-5", "", None):
        assert is_reasoning_model(model) is False
    assert "reasoning families" in repair_lines("gpt-4o")[0]


def test_a_quiet_row_is_not_a_silent_one():
    assert silent_share({"requests": 0, "silent_requests": 0}) is None
    assert silent_share(None) is None
    state, _ = classify("gpt-5.1", {"requests": 4, "silent_requests": 4})
    assert state == "too-few-requests"
    healthy = fold([bucket("p", "gpt-5.1", 500, 200000, 60000)])
    assert classify("gpt-5.1", healthy[("p", "gpt-5.1")])[0] == "generating"


def test_a_404_on_the_model_lookup_is_a_different_note_entirely():
    assert model_verdict(200)[0] == "id-resolves"
    assert model_verdict(404)[0] == "id-unreachable"
    assert "retirement or entitlement" in model_verdict(404)[1]
    assert model_verdict(403)[0] == "check-refused"
    assert model_verdict(None)[0] == "unchecked"


def test_unreadable_usage_fields_do_not_become_phantom_requests():
    rows = fold([{"results": [{"project_id": "p", "model": "gpt-5.1",
                               "num_model_requests": None,
                               "input_tokens": "nonsense",
                               "output_tokens": None}]}])
    assert rows[("p", "gpt-5.1")]["requests"] == 0
    assert rows[("p", "gpt-5.1")]["silent_buckets"] == 0
    assert fold([]) == {}
    assert fold(None) == {}
