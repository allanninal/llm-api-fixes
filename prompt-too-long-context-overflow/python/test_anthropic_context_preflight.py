from anthropic_context_preflight import (batch_overflows, budget, count_body,
                                          turns_remaining, verdict, window_of)


def test_input_fits_but_the_reservation_does_not():
    # The whole note in two assertions. 190k of input under a 200k window is
    # fine; the same input with a routine max_tokens is over, and it is over in
    # the way that comes back as a 200 rather than as a 400.
    ok_state, _ = verdict(190_000, 0, 200_000)
    assert ok_state == "window-tight"

    state, detail = verdict(190_000, 16_000, 200_000)
    assert state == "budget-over-window"
    assert "190000 input + 16000 max_tokens = 206000 of a 200000 token window" in detail
    assert "model_context_window_exceeded" in detail
    assert "200" in detail


def test_input_alone_over_the_window_is_the_other_failure():
    state, detail = verdict(260_000, 4_000, 200_000)
    assert state == "input-over-window"
    assert "prompt is too long" in detail
    assert budget(260_000, 4_000) == 264_000


def test_a_comfortable_payload_is_not_a_finding():
    state, detail = verdict(40_000, 8_000, 200_000)
    assert state == "fits"
    assert "(24%)" in detail


def test_the_counting_endpoint_only_gets_the_keys_it_accepts():
    body = {"model": "claude-sonnet-5", "system": "s", "messages": [],
            "tools": [{"name": "t"}], "tool_choice": {"type": "auto"},
            "thinking": {"type": "enabled"}, "max_tokens": 16_000,
            "temperature": 0.2, "stream": True, "service_tier": "auto"}
    trimmed = count_body(body)
    # Sampling parameters out, because count_tokens 400s on them.
    assert "max_tokens" not in trimmed
    assert "temperature" not in trimmed
    assert "stream" not in trimmed
    assert "service_tier" not in trimmed
    # Everything that occupies the window stays, because dropping any of it
    # would count a request you are not sending.
    assert set(trimmed) == {"model", "system", "messages", "tools",
                            "tool_choice", "thinking"}
    assert count_body(None) == {}


def test_a_missing_window_is_not_an_infinite_one():
    assert window_of({"id": "claude-sonnet-5", "max_input_tokens": 200_000}) == 200_000
    assert window_of({"id": "claude-sonnet-5"}) is None
    assert window_of({"max_input_tokens": 0}) is None
    assert window_of({"max_input_tokens": "200000"}) is None
    assert window_of(None) is None
    state, detail = verdict(500_000, 8_000, None)
    assert state == "window-unknown"
    assert "no max_input_tokens" in detail


def test_turns_remaining_is_the_number_a_product_team_wants():
    assert turns_remaining(120_000, 16_000, 200_000, 1_800) == 35
    assert turns_remaining(199_000, 16_000, 200_000, 1_800) == 0
    assert turns_remaining(120_000, 16_000, None, 1_800) is None
    assert turns_remaining(120_000, 16_000, 200_000, 0) is None


def test_batch_results_yield_both_shapes_keyed_by_custom_id():
    lines = [
        '{"custom_id": "doc-9", "result": {"type": "succeeded", "message": '
        '{"stop_reason": "model_context_window_exceeded"}}}',
        '{"custom_id": "doc-3", "result": {"type": "errored", "error": '
        '{"type": "invalid_request_error", "message": "prompt is too long: '
        '412000 tokens > 200000 maximum"}}}',
        '{"custom_id": "doc-1", "result": {"type": "succeeded", "message": '
        '{"stop_reason": "end_turn"}}}',
        "",
        "not json at all",
    ]
    assert batch_overflows(lines) == {"doc-9": "truncated-with-200",
                                      "doc-3": "rejected-with-400"}
    assert batch_overflows([]) == {}
    assert batch_overflows(None) == {}
