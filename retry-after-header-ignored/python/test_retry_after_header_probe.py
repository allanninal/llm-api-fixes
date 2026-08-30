from retry_after_header_probe import (clock_skew, compare, lower_headers,
                                      missing, parse_reset, repair_lines,
                                      stale_resets, verdict)

ANTHROPIC_OK = {
    "Anthropic-Ratelimit-Requests-Limit": "1000",
    "anthropic-ratelimit-requests-remaining": "998",
    "anthropic-ratelimit-requests-reset": "2026-08-31T09:12:00Z",
    "anthropic-ratelimit-input-tokens-limit": "10000000",
    "anthropic-ratelimit-input-tokens-remaining": "9998000",
    "anthropic-ratelimit-input-tokens-reset": "2026-08-31T09:12:00Z",
    "anthropic-ratelimit-output-tokens-limit": "2000000",
    "anthropic-ratelimit-output-tokens-remaining": "1999000",
    "anthropic-ratelimit-output-tokens-reset": "2026-08-31T09:12:00Z",
    "anthropic-ratelimit-tokens-limit": "12000000",
    "anthropic-ratelimit-tokens-remaining": "11997000",
    "anthropic-ratelimit-tokens-reset": "2026-08-31T09:12:00Z",
    "date": "Mon, 31 Aug 2026 09:11:00 GMT",
}


def without(headers, prefix):
    return {k: v for k, v in headers.items()
            if not k.lower().startswith(prefix)}


def test_a_gateway_that_drops_the_triples_is_the_finding():
    # The note. Header casing differs between the two paths on purpose: a
    # comparison that does not normalise reports every proxy as stripping.
    gateway = without(ANTHROPIC_OK, "anthropic-ratelimit-input")
    rows = compare(ANTHROPIC_OK, gateway, "anthropic")
    stripped = [n for n, (_, _, s) in rows.items() if s == "stripped"]
    assert stripped == ["anthropic-ratelimit-input-tokens-limit",
                        "anthropic-ratelimit-input-tokens-remaining",
                        "anthropic-ratelimit-input-tokens-reset"]
    state, detail = verdict(rows, [], True, 0.0, [])
    assert state == "headers-stripped"
    assert "do not survive the gateway" in detail
    lines = repair_lines(state, "anthropic", stripped)
    assert any("retry-after travels with these" in line for line in lines)
    assert any("allowlist" in line for line in lines)


def test_remaining_may_differ_across_paths_but_a_limit_may_not():
    # Two calls a second apart. remaining and reset are supposed to move, so
    # comparing them would make every healthy path look rewritten.
    later = dict(ANTHROPIC_OK)
    later["anthropic-ratelimit-requests-remaining"] = "997"
    later["anthropic-ratelimit-requests-reset"] = "2026-08-31T09:12:01Z"
    rows = compare(ANTHROPIC_OK, later, "anthropic")
    assert rows["anthropic-ratelimit-requests-remaining"][2] == "intact"
    assert rows["anthropic-ratelimit-requests-reset"][2] == "intact"
    assert verdict(rows, [], True, 0.0, [])[0] == "headers-intact"

    faked = dict(ANTHROPIC_OK)
    faked["anthropic-ratelimit-requests-limit"] = "50"
    rows = compare(ANTHROPIC_OK, faked, "anthropic")
    assert rows["anthropic-ratelimit-requests-limit"][2] == "rewritten"
    state, detail = verdict(rows, [], True, 0.0, [])
    assert state == "headers-rewritten"
    assert "generating headers rather than forwarding" in detail
    assert any("more dangerous than stripping" in line
               for line in repair_lines(state))


def test_the_two_reset_formats_are_told_apart_rather_than_guessed():
    kind, value = parse_reset("2026-08-31T09:12:00Z")
    assert kind == "absolute" and value == 1788167520.0
    assert parse_reset("6m0s") == ("duration", 360.0)
    assert parse_reset("30s") == ("duration", 30.0)
    assert parse_reset("1h2m3s") == ("duration", 3723.0)
    assert parse_reset("500ms") == ("duration", 0.5)
    assert parse_reset("12") == ("duration", 12.0)
    assert parse_reset("") == ("unknown", None)
    assert parse_reset("soon") == ("unknown", None)


def test_the_clock_is_read_against_the_server_and_a_stale_reset_is_its_own_state():
    # 09:11:00 on the server, 09:11:42 locally: 42 seconds ahead.
    skew = clock_skew("Mon, 31 Aug 2026 09:11:00 GMT", 1788167502.0)
    assert round(skew) == 42
    assert clock_skew("", 0) is None and clock_skew("not a date", 0) is None
    state, detail = verdict(compare(ANTHROPIC_OK, ANTHROPIC_OK, "anthropic"),
                            [], True, skew, [])
    assert state == "clock-skew"
    assert "ahead of" in detail
    assert any("RFC 3339 instants" in line
               for line in repair_lines(state, "anthropic"))
    # Reset instants already elapsed on the server's own clock.
    stale = stale_resets(ANTHROPIC_OK, "anthropic", 1788167600.0)
    assert len(stale) == 4 and stale[0][1] == 80.0
    assert verdict(compare(ANTHROPIC_OK, ANTHROPIC_OK, "anthropic"),
                   [], True, 0.0, stale)[0] == "reset-in-the-past"


def test_a_transport_failure_is_reported_before_a_clock_one():
    gateway = without(ANTHROPIC_OK, "anthropic-ratelimit-input")
    rows = compare(ANTHROPIC_OK, gateway, "anthropic")
    stale = stale_resets(ANTHROPIC_OK, "anthropic", 1788167600.0)
    # Stripped headers, a stale reset and a large skew all at once. There is
    # nothing to compute a sleep from, so the transport answer comes first.
    assert verdict(rows, [], True, 300.0, stale)[0] == "headers-stripped"


def test_openai_headers_and_the_no_gateway_case():
    openai = {"x-ratelimit-limit-requests": "10000",
              "x-ratelimit-remaining-requests": "9999",
              "x-ratelimit-reset-requests": "6m0s",
              "x-ratelimit-limit-tokens": "2000000",
              "x-ratelimit-remaining-tokens": "1999000",
              "x-ratelimit-reset-tokens": "6m0s"}
    assert missing(openai, "openai") == []
    assert stale_resets(openai, "openai", 1788167600.0) == []
    assert verdict(compare(openai, openai, "openai"), [], False, 0.0, [])[0] \
        == "headers-intact"
    bare = missing({}, "openai")
    assert len(bare) == 6
    state, detail = verdict(compare({}, {}, "openai"), bare, False, None, [])
    assert state == "headers-absent"
    assert "no gateway configured to blame" in detail
    assert any("not attributable yet" in line for line in repair_lines(state))
    assert lower_headers(None) == {} and repair_lines("headers-intact") == []
