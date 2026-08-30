import datetime as dt

from openai_quota_wall_audit import classify, error_fields, headroom, stalled

NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)


def hours_ago(h):
    return int(NOW.timestamp() - h * 3600)


def bucket(h, requests=10, output=4000):
    return {"start_time": hours_ago(h),
            "results": [{"num_model_requests": requests, "input_tokens": 900,
                         "output_tokens": output}]}


def openai_error(code, status_message="You exceeded your current quota."):
    return {"error": {"message": status_message, "type": "insufficient_quota",
                      "code": code}}


def test_error_fields_reads_nested_and_bare_envelopes():
    assert error_fields(openai_error("insufficient_quota"))[0] == "insufficient_quota"
    assert error_fields({"code": "rate_limit_exceeded"})[0] == "rate_limit_exceeded"
    assert error_fields(None) == ("", "", "")
    assert error_fields({"error": "a string, not an object"})[0] == ""


def test_the_whole_point_two_429s_that_are_not_the_same_thing():
    wall, wall_detail = classify(429, openai_error("insufficient_quota"))
    throttle, _ = classify(429, openai_error("rate_limit_exceeded"))
    assert wall == "wall"
    assert throttle == "throttle"
    assert "RateLimitError" in wall_detail


def test_every_billing_code_is_a_wall_with_its_own_remedy():
    remedies = {}
    for code in ("credit_balance_exhausted", "organization_spend_limit_exceeded",
                 "project_spend_limit_exceeded", "organization_usage_limit_exceeded"):
        state, detail = classify(429, openai_error(code))
        assert state == "wall", code
        remedies[code] = detail
    # Four different consoles. Printing one message for all four sends the
    # on-call engineer to the wrong place.
    assert len(set(remedies.values())) == 4


def test_an_unrecognised_429_code_is_not_retried_blindly():
    state, detail = classify(429, openai_error("some_new_code_2027"))
    assert state == "unclassified-429"
    assert "not retryable" in detail


def test_a_429_with_no_code_at_all_is_still_not_a_free_retry_loop():
    assert classify(429, {"error": {"message": "Too many requests"}})[0] == "unclassified-429"


def test_anthropic_429_matches_on_type_because_it_has_no_code():
    state, _ = classify(429, {"type": "error",
                              "error": {"type": "rate_limit_error",
                                        "message": "Number of requests has exceeded"}})
    assert state == "throttle"


def test_anthropic_puts_the_same_wall_behind_a_400():
    state, detail = classify(400, {"error": {
        "type": "invalid_request_error",
        "message": "Your credit balance is too low to access the Claude API."}})
    assert state == "wall"
    assert "400" in detail


def test_auth_and_server_errors_are_not_confused_with_either():
    assert classify(401, {})[0] == "auth"
    assert classify(503, {})[0] == "transient"
    assert classify(404, {})[0] == "other"


def test_headroom_forecasts_the_one_wall_that_can_be_forecast():
    assert headroom(120.0, None)[0] == "tier-unknown"
    assert headroom(120.0, 1000.0)[0] == "clear"
    assert headroom(850.0, 1000.0)[0] == "approaching"
    assert headroom(1000.0, 1000.0)[0] == "at-ceiling"


def test_stalled_reads_a_cliff_against_the_clock_it_is_given():
    fresh = stalled([bucket(30), bucket(2)], NOW)
    assert fresh[0] == "flowing"
    state, detail = stalled([bucket(30), bucket(20)], NOW)
    assert state == "cliff"
    assert "20.0 hour(s) ago" in detail


def test_requests_with_no_output_is_a_different_finding_from_a_cliff():
    # A bucket that made calls and generated nothing is an error shape. Folding
    # it into the cliff sends you looking for a billing problem that is not there.
    state, _ = stalled([bucket(20, requests=40, output=0), bucket(1)], NOW)
    assert state == "failing-before-generation"


def test_empty_and_silent_windows_do_not_claim_a_wall():
    assert stalled([], NOW)[0] == "no-data"
    assert stalled([bucket(3, requests=0, output=0)], NOW)[0] == "no-data"
