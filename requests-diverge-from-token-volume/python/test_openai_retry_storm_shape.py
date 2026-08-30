from openai_retry_storm_shape import (burstiness, classify, divergence_ratio,
                                       fold_windows, growth, limiter_pressure,
                                       rate_limit_values, series,
                                       tokens_per_request)

CUTOFF = 1_000_000
HOUR = 3600


def hours(start, count, requests_each, tokens_each):
    return [{"start": start + i * HOUR, "requests": requests_each,
             "tokens": tokens_each} for i in range(count)]


PRIOR_WEEK = hours(CUTOFF - 168 * HOUR, 168, 1000, 5_000_000)
# Same weekly totals, two different shapes. The storm piles its surplus into
# eighteen hours; the short workload spreads the identical surplus evenly.
STORM = (hours(CUTOFF, 150, 1000, 5_000_000)
         + hours(CUTOFF + 150 * HOUR, 18, 20_000, 5_000_000))
EVEN = hours(CUTOFF, 168, 3000, 5_000_000)


def test_requests_climb_in_bursts_while_tokens_stand_still():
    # The note in one assertion. Three times the calls, no more tokens, the
    # mean call down from 5000 to 1647, and two thirds of the surplus landing
    # in the busiest tenth of the hours.
    prior, recent = fold_windows(PRIOR_WEEK + STORM, CUTOFF)
    assert prior["requests"] == 168_000 and prior["tokens"] == 840_000_000
    assert recent["requests"] == 510_000 and recent["tokens"] == 840_000_000
    assert round(growth(prior["requests"], recent["requests"]), 3) == 3.036
    assert growth(prior["tokens"], recent["tokens"]) == 1.0
    assert int(tokens_per_request(prior)) == 5000
    assert int(tokens_per_request(recent)) == 1647

    burst = burstiness(PRIOR_WEEK + STORM, CUTOFF)
    assert round(burst, 3) == 0.667
    state, detail = classify(prior, recent, burst)
    assert state == "retry-storm"
    assert "requests x3.04, tokens x1.00" in detail
    assert "tokens per request 5000 then 1647" in detail
    assert "67% of the surplus landed in the busiest 10% of hours" in detail


def test_the_same_ratios_spread_evenly_are_not_a_storm():
    # Identical weekly arithmetic, opposite conclusion. This pair is the reason
    # the concentration measure exists at all.
    prior, recent = fold_windows(PRIOR_WEEK + EVEN, CUTOFF)
    assert round(divergence_ratio(prior, recent), 2) == 3.0
    burst = burstiness(PRIOR_WEEK + EVEN, CUTOFF)
    assert round(burst, 3) == 0.101
    state, detail = classify(prior, recent, burst)
    assert state == "requests-outpacing-tokens"
    assert "spread evenly across the hours" in detail


def test_the_divergence_ratio_is_the_mean_call_size_inverted():
    # Stated as a test because the first draft treated these as two agreeing
    # signals. They cannot disagree.
    prior, recent = fold_windows(PRIOR_WEEK + STORM, CUTOFF)
    identity = tokens_per_request(prior) / tokens_per_request(recent)
    assert round(divergence_ratio(prior, recent), 9) == round(identity, 9)


def test_a_real_customer_moves_both_series_together():
    state, detail = classify({"requests": 100_000, "tokens": 500_000_000},
                             {"requests": 300_000, "tokens": 1_500_000_000}, 0.1)
    assert state == "traffic-growth"
    assert "moved together" in detail


def test_a_prompt_that_grew_moves_only_the_token_series():
    state, _ = classify({"requests": 100_000, "tokens": 200_000_000},
                        {"requests": 100_000, "tokens": 600_000_000}, 0.1)
    assert state == "prompts-grew"


def test_the_partial_hour_is_dropped_before_anything_is_divided():
    tail = [{"start": CUTOFF + 200 * HOUR, "requests": 1, "tokens": 10}]
    prior, recent = fold_windows(PRIOR_WEEK + EVEN + tail, CUTOFF,
                                 partial_after=CUTOFF + 200 * HOUR)
    assert recent["buckets"] == 168 and recent["requests"] == 504_000
    assert burstiness(EVEN + tail, CUTOFF, partial_after=CUTOFF + 200 * HOUR) \
        == burstiness(EVEN, CUTOFF)


def test_a_workload_with_no_prior_week_has_no_growth_rate():
    assert growth(0, 5000) is None
    assert growth(None, 5000) is None
    assert divergence_ratio({"requests": 1, "tokens": 0},
                            {"requests": 2, "tokens": 0}) is None
    assert tokens_per_request({"requests": 0, "tokens": 0}) is None
    state, _ = classify({"requests": 0, "tokens": 0},
                        {"requests": 40_000, "tokens": 90_000_000})
    assert state == "new-workload"
    assert classify({"requests": 10, "tokens": 900},
                    {"requests": 12, "tokens": 1000})[0] == "too-little-traffic"


def test_too_few_hours_reports_no_concentration_rather_than_a_wrong_one():
    short = [{"start": CUTOFF + i * HOUR, "requests": 10, "tokens": 1}
             for i in range(6)]
    assert burstiness(short, CUTOFF) is None
    assert burstiness([], CUTOFF) is None
    state, detail = classify({"requests": 100_000, "tokens": 500_000_000},
                             {"requests": 400_000, "tokens": 520_000_000})
    assert state == "retry-storm"
    assert "Too few hourly buckets" in detail


def test_the_request_bucket_is_full_while_the_token_bucket_is_empty():
    payload = {"data": [{"model": "gpt-5.1", "max_requests_per_1_minute": 10_000,
                         "max_tokens_per_1_minute": 20_000_000},
                        {"model": "gpt-5", "max_requests_per_1_minute": 1,
                         "max_tokens_per_1_minute": 1}]}
    limits = rate_limit_values(payload, "gpt-5.1-2026-01-15")
    assert limits == {"requests": 10_000, "tokens": 20_000_000}

    state, detail = limiter_pressure(
        {"requests": 82_656_000, "tokens": 18_144_000_000}, 168, limits)
    assert state == "rpm-bound-tpm-idle"
    assert "82% of the RPM ceiling and 9% of the TPM ceiling" in detail


def test_an_unpublished_limit_is_not_a_missing_one():
    assert rate_limit_values({"data": []}, "gpt-5.1") == {"requests": None,
                                                          "tokens": None}
    assert limiter_pressure({"requests": 1}, 24, None)[0] == "no-limits-published"
    assert series([]) == {}
    assert series(None) == {}
