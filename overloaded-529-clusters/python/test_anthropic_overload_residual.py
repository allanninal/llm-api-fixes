from anthropic_overload_residual import (attempts_by_minute,
                                          baseline_tokens_per_attempt,
                                          classify, clusters, excess_minutes,
                                          minute_index, minute_key,
                                          residual_rows, tiers_seen,
                                          tokens_by_minute)


def minute(n):
    return "2026-08-30T14:%02dZ" % n


# Ten minutes at 600 attempts each. Seven of them do the full 3,000,000 tokens
# of work that 600 calls at 5000 tokens implies; minutes 4, 5 and 6 do a fifth
# of it, because the platform was over capacity and served 120 of the 600.
ATTEMPTS = {minute(n): 600 for n in range(10)}
TOKENS = {minute(n): (600_000 if n in (4, 5, 6) else 3_000_000) for n in range(10)}


def test_three_adjacent_bad_minutes_are_one_overload_cluster():
    baseline = baseline_tokens_per_attempt(TOKENS, ATTEMPTS)
    # The median survives the outage. A mean over the same data is 3800, which
    # would hide most of the loss it was computed to find.
    assert baseline == 5000.0

    rows = residual_rows(TOKENS, ATTEMPTS, baseline)
    assert len(rows) == 10
    bad = [r for r in rows if r["share"] > 0.5]
    assert [r["minute"] for r in bad] == [minute(4), minute(5), minute(6)]
    assert round(bad[0]["residual"]) == 480

    runs = clusters(rows)
    assert len(runs) == 1
    state, detail = classify(runs[0])
    assert state == "overload-cluster"
    assert "2026-08-30T14:04Z through 2026-08-30T14:06Z" in detail
    assert "1800 attempt(s) over 3 minute(s)" in detail
    assert "about 1440 of them produced no billed tokens (80%)" in detail


def test_one_bad_minute_on_its_own_is_bucket_arithmetic():
    attempts = dict(ATTEMPTS)
    tokens = {k: 3_000_000 for k in attempts}
    tokens[minute(4)] = 600_000
    rows = residual_rows(tokens, attempts,
                         baseline_tokens_per_attempt(tokens, attempts))
    runs = clusters(rows)
    assert len(runs) == 1 and len(runs[0]) == 1
    state, detail = classify(runs[0])
    assert state == "single-minute-dip"
    assert "straddled a bucket boundary" in detail


def test_minutes_that_are_not_adjacent_do_not_become_one_cluster():
    tokens = {k: 3_000_000 for k in ATTEMPTS}
    for n in (1, 5, 9):
        tokens[minute(n)] = 100_000
    rows = residual_rows(tokens, ATTEMPTS,
                         baseline_tokens_per_attempt(tokens, ATTEMPTS))
    assert [len(run) for run in clusters(rows)] == [1, 1, 1]


def test_the_two_clocks_are_normalised_to_the_same_minute():
    # Anthropic returns full RFC 3339; your counter probably does not. Two
    # formats that never match produce a clean report during a real incident.
    for stamp in ("2026-08-30T14:03:27Z", "2026-08-30T14:03Z",
                  "2026-08-30 14:03:00+00:00", "2026-08-30T14:03:59.512Z"):
        assert minute_key(stamp) == "2026-08-30T14:03Z"
    assert minute_key(1788098580) == "2026-08-30T14:03Z"
    assert minute_key("last tuesday") is None
    assert minute_key("") is None
    assert minute_key(None) is None
    assert minute_key(True) is None
    # Adjacency is arithmetic, not string order: 14:59 and 15:00 are neighbours.
    assert minute_index("2026-08-30T15:00Z") - minute_index("2026-08-30T14:59Z") == 1


def test_the_nested_cache_creation_object_is_counted_as_work():
    buckets = [{"starting_at": "2026-08-30T14:03:00Z",
                "results": [{"uncached_input_tokens": 10,
                             "cache_read_input_tokens": 20,
                             "output_tokens": 5,
                             "service_tier": "standard",
                             "cache_creation": {"ephemeral_5m_input_tokens": 100,
                                                "ephemeral_1h_input_tokens": 65}}]}]
    assert tokens_by_minute(buckets) == {"2026-08-30T14:03Z": 200}
    assert tiers_seen(buckets) == {"standard"}
    assert tokens_by_minute([]) == {}


def test_an_attempt_file_is_read_leniently_and_bad_keys_are_dropped():
    assert attempts_by_minute({"2026-08-30T14:03:00Z": 900}) == {"2026-08-30T14:03Z": 900}
    assert attempts_by_minute({"2026-08-30T14:03Z": {"attempts": 900}}) \
        == {"2026-08-30T14:03Z": 900}
    assert attempts_by_minute({"whenever": 900}) == {}
    assert attempts_by_minute(None) == {}


def test_more_work_than_the_attempts_explain_is_the_other_note():
    attempts = {minute(n): 100 for n in range(10)}
    tokens = {minute(n): 500_000 for n in range(10)}
    tokens[minute(3)] = 2_000_000
    baseline = baseline_tokens_per_attempt(tokens, attempts)
    rows = residual_rows(tokens, attempts, baseline)
    assert excess_minutes(rows) == [minute(3)]
    assert clusters(rows) == []


def test_too_little_overlap_produces_no_baseline_rather_than_a_guess():
    assert baseline_tokens_per_attempt({}, {}) is None
    assert baseline_tokens_per_attempt({minute(0): 5000}, {minute(0): 1}) is None
    assert baseline_tokens_per_attempt({}, ATTEMPTS) is None
    assert residual_rows(TOKENS, ATTEMPTS, None) == []
    assert classify([])[0] == "no-cluster"
