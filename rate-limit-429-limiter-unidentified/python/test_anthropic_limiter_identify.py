import datetime as dt

from anthropic_limiter_identify import (configured, emptiest, log_headers,
                                        mirrors, parse_count, read_triples,
                                        seconds_until, share_left, verdict)

NOW = dt.datetime(2026, 8, 30, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_the_aggregate_ceiling_names_the_binding_limiter():
    # Sonnet-shaped numbers: ITPM five million, OTPM four hundred thousand. The
    # aggregate equals the output ceiling, which is Anthropic telling you which
    # bucket is tightest without anything having to infer it.
    parsed = read_triples({
        "anthropic-ratelimit-requests-limit": "4000",
        "anthropic-ratelimit-requests-remaining": "3600",
        "anthropic-ratelimit-input-tokens-limit": "5000000",
        "anthropic-ratelimit-input-tokens-remaining": "4000000",
        "anthropic-ratelimit-output-tokens-limit": "400000",
        "anthropic-ratelimit-output-tokens-remaining": "12000",
        "anthropic-ratelimit-tokens-limit": "400000",
        "anthropic-ratelimit-tokens-remaining": "12000",
    })
    assert mirrors(parsed) == "output-tokens"
    assert emptiest(parsed) == ("output-tokens", 0.03)
    state, detail = verdict(parsed)
    assert state == "identified"
    assert "output-tokens is the emptiest named bucket at 3% remaining" in detail


def test_the_tightest_ceiling_and_the_emptiest_bucket_can_disagree():
    # The request bucket is nearly gone while the output ceiling is the lower
    # number. Reporting either alone names the wrong cause.
    parsed = read_triples({
        "anthropic-ratelimit-requests-limit": "4000",
        "anthropic-ratelimit-requests-remaining": "40",
        "anthropic-ratelimit-input-tokens-limit": "5000000",
        "anthropic-ratelimit-input-tokens-remaining": "4900000",
        "anthropic-ratelimit-output-tokens-limit": "400000",
        "anthropic-ratelimit-output-tokens-remaining": "380000",
        "anthropic-ratelimit-tokens-limit": "400000",
        "anthropic-ratelimit-tokens-remaining": "380000",
    })
    state, detail = verdict(parsed)
    assert state == "disagreement"
    assert "requests is the emptiest named bucket at 1% remaining" in detail
    assert "mirrors output-tokens" in detail


def test_an_aggregate_matching_neither_ceiling_is_a_third_limit():
    parsed = read_triples({
        "anthropic-ratelimit-requests-limit": "4000",
        "anthropic-ratelimit-requests-remaining": "3900",
        "anthropic-ratelimit-input-tokens-limit": "5000000",
        "anthropic-ratelimit-input-tokens-remaining": "4900000",
        "anthropic-ratelimit-output-tokens-limit": "400000",
        "anthropic-ratelimit-output-tokens-remaining": "390000",
        "anthropic-ratelimit-tokens-limit": "150000",
        "anthropic-ratelimit-tokens-remaining": "150000",
    })
    assert mirrors(parsed) == "unmatched"
    assert verdict(parsed)[0] == "aggregate-unmatched"


def test_no_headers_is_a_finding_and_not_a_pass():
    assert read_triples({"content-type": "application/json"}) == {}
    assert read_triples(None) == {}
    state, detail = verdict({})
    assert state == "headers-missing"
    assert "retry-after would be missing too" in detail
    assert log_headers({"content-type": "application/json"}) == []


def test_the_aggregate_never_competes_to_be_the_emptiest_bucket():
    # Otherwise the same limiter is reported twice under two names, and the
    # bucket the aggregate is not mirroring disappears from the report.
    parsed = {"requests": {"limit": 100, "remaining": 90},
              "output-tokens": {"limit": 1000, "remaining": 500},
              "tokens": {"limit": 1000, "remaining": 1}}
    assert emptiest(parsed) == ("output-tokens", 0.5)


def test_absent_and_empty_are_different_readings():
    assert parse_count(None) is None
    assert parse_count("") is None
    assert parse_count("0") == 0
    assert parse_count("2,000,000") == 2000000
    assert parse_count("lots") is None
    assert share_left({"limit": 100, "remaining": None}) is None
    assert share_left({"limit": 0, "remaining": 0}) is None
    assert verdict({"requests": {"limit": None, "remaining": None}})[0] == "unreadable"


def test_rfc3339_resets_parse_and_unreadable_ones_stay_unreadable():
    assert seconds_until("2026-08-30T12:00:30Z", NOW) == 30.0
    assert seconds_until("2026-08-30T12:00:30+00:00", NOW) == 30.0
    assert seconds_until("in a bit", NOW) is None
    assert seconds_until("", NOW) is None
    assert seconds_until(None, NOW) is None


def test_an_unpublished_limiter_is_not_an_unlimited_one():
    payload = {"data": [
        {"model_group": "claude-sonnet-5", "limits": [
            {"type": "requests_per_minute", "value": 4000},
            {"type": "input_tokens_per_minute", "value": 5000000},
            {"type": "output_tokens_per_minute", "value": 1000000}]},
        {"model_group": "message-batches", "limits": [
            {"type": "requests_per_minute", "value": 100}]},
    ]}
    folded = configured(payload)
    assert folded["claude-sonnet-5"]["output_tokens_per_minute"] == 1000000
    # Absent from limits[] means it inherits, so it must read as None and get
    # printed as unpublished rather than silently becoming zero or infinity.
    assert folded["message-batches"]["input_tokens_per_minute"] is None
    assert folded["message-batches"]["requests_per_minute"] == 100
    assert configured({}) == {}
    assert configured(None) == {}


def test_the_repair_lists_only_headers_that_actually_arrived():
    names = log_headers({
        "Anthropic-RateLimit-Output-Tokens-Remaining": "12000",
        "anthropic-ratelimit-tokens-limit": "400000",
        "Retry-After": "12",
        "request-id": "req_fake123",
        "content-type": "application/json",
    })
    assert names == ["anthropic-ratelimit-output-tokens-remaining",
                     "anthropic-ratelimit-tokens-limit",
                     "request-id", "retry-after"]
