from openai_rate_limit_headroom import (binding, headroom, parse_count,
                                        parse_reset, scope_note, triples,
                                        verdict)


def test_token_headroom_is_the_finding_while_requests_look_fine():
    # The note in one assertion: 4% of tokens left, 91% of requests, and the
    # report has to name tokens. An average of the two says 47% and says it
    # about nothing. The mixed casing is deliberate: gateways rewrite it.
    parsed = triples({
        "X-RateLimit-Limit-Requests": "10000",
        "X-RateLimit-Remaining-Requests": "9100",
        "X-RateLimit-Reset-Requests": "6m0s",
        "x-ratelimit-limit-tokens": "200000",
        "x-ratelimit-remaining-tokens": "8000",
        "x-ratelimit-reset-tokens": "47s",
    })
    assert verdict("requests", parsed["requests"])[0] == "headroom"
    state, detail = verdict("tokens", parsed["tokens"])
    assert state == "near-exhaustion"
    assert "8000 of 200000 left (4%), resets in 47s" in detail
    assert binding(parsed) == ("tokens", 0.04)


def test_an_empty_bucket_is_reported_before_any_429_arrives():
    state, detail = verdict("tokens", {"limit": 200000, "remaining": 0, "reset": 12.0})
    assert state == "exhausted"
    assert "empty now" in detail


def test_absent_headers_are_not_an_empty_bucket():
    # parse_count must distinguish "the gateway stripped it" from "you are out".
    assert parse_count(None) is None
    assert parse_count("") is None
    assert parse_count("0") == 0
    assert parse_count("1,500,000") == 1500000
    assert parse_count("not a number") is None
    assert headroom({"limit": 200000, "remaining": None}) is None
    assert verdict("tokens", {"limit": 200000, "remaining": None})[0] == "unreadable"


def test_go_duration_resets_parse_and_ms_is_not_minutes():
    assert parse_reset("500ms") == 0.5
    assert parse_reset("6m0s") == 360.0
    assert parse_reset("1h2m3s") == 3723.0
    assert parse_reset("47s") == 47.0
    # Formats this parser does not understand must not half-parse into a number
    # a reader would then act on.
    assert parse_reset("60 seconds") is None
    assert parse_reset("soon") is None
    assert parse_reset("") is None
    assert parse_reset(None) is None


def test_a_probe_with_no_rate_limit_headers_parses_to_nothing():
    # Which main() reports as its own finding rather than as a clean run.
    assert triples({"content-type": "application/json"}) == {}
    assert triples({}) == {}
    assert triples(None) == {}
    assert binding({}) is None


def test_the_project_ceiling_is_the_real_ceiling_when_it_is_lower():
    parsed = triples({
        "x-ratelimit-limit-tokens": "200000",
        "x-ratelimit-remaining-tokens": "150000",
        "x-ratelimit-limit-project-tokens": "150000",
        "x-ratelimit-remaining-project-tokens": "12000",
        "x-ratelimit-reset-project-tokens": "30s",
    })
    assert scope_note(parsed) == [("project", "tokens", 150000, 200000)]
    # And the org triple, read alone, would have said everything was fine.
    assert verdict("tokens", parsed["tokens"])[0] == "headroom"
    assert verdict("project-tokens", parsed["project-tokens"])[0] == "near-exhaustion"
    assert binding(parsed)[0] == "project-tokens"


def test_scope_note_says_nothing_when_there_is_nothing_to_compare():
    assert scope_note(triples({
        "x-ratelimit-limit-tokens": "200000",
        "x-ratelimit-remaining-tokens": "150000",
    })) == []
    assert scope_note({}) == []
    assert scope_note(None) == []
