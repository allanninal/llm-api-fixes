from openai_cache_key_routing_scatter import (cached_share, classify,
                                              continuation_rows, handoff,
                                              hour_index, hour_label,
                                              load_split, resumption_rows,
                                              rows_by_series, spearman)

BASE = hour_index("2026-08-17T00:00Z")

# A daily traffic shape: quiet overnight, a broad afternoon peak.
LOAD = [200, 150, 120, 100, 120, 200, 400, 900, 1800, 3000, 3600, 3800,
        3900, 3800, 3600, 3000, 2400, 1800, 1200, 800, 600, 450, 350, 260]


def hour(offset, requests, share):
    tokens = requests * 2000
    return {"index": BASE + offset, "hour": hour_label(BASE + offset),
            "requests": requests, "input": tokens,
            "cached": int(round(tokens * share))}


def contiguous(share_of_load):
    """Seven contiguous days on the daily load shape, no gaps anywhere."""
    return [hour(i, LOAD[i % 24], share_of_load(LOAD[i % 24])) for i in range(168)]


# The note. The prefix is provably cacheable, because the quiet hours cache
# beautifully; the share falls away as the fleet fans out.
SCATTER = contiguous(lambda load: 0.72 - 0.00016 * load)
# Identical traffic, a share that ignores the load entirely. Prefix instability.
FLAT = contiguous(lambda _load: 0.10)
# Identical traffic, a share that improves with density. Not a fault.
RISING = contiguous(lambda load: 0.10 + 0.00016 * load)


def bursty():
    """Three-hour bursts five hours apart, each opening at full tilt.

    The busiest hour of every burst is also the coldest one, because it is the
    hour that follows the idle stretch. That is the trap: correlate the raw
    series and the load looks guilty.
    """
    shape = [(3000, 0.0), (1000, 0.7), (400, 0.7)]
    rows = []
    for burst in range(21):
        for step, (requests, share) in enumerate(shape):
            rows.append(hour(burst * 8 + step, requests, share))
    return rows


BURSTY = bursty()


def test_the_cached_share_falling_with_load_is_the_finding():
    # The note in one assertion: same prefix, same code, and the discount
    # evaporates exactly when the fleet is widest.
    quiet, busy, quiet_rate, busy_rate = load_split(SCATTER)
    assert round(quiet, 2) == 0.69 and round(busy, 2) == 0.16
    assert quiet_rate < 300 and busy_rate > 3400

    rho = spearman([r["requests"] for r in SCATTER],
                   [cached_share([r]) for r in SCATTER])
    assert round(rho, 3) == -1.0

    state, detail = classify(SCATTER)
    assert state == "load-correlated-misses"
    assert "68% in the quietest hours" in detail
    assert "16% in the busiest" in detail
    assert "rank correlation -1.00" in detail
    assert handoff(state) == ""


def test_the_same_traffic_with_a_flat_share_is_the_prefix_note():
    # The control that makes the finding mean something. Byte-identical load,
    # a share that does not move with it, and a different note owns it.
    assert [r["requests"] for r in FLAT] == [r["requests"] for r in SCATTER]
    assert spearman([r["requests"] for r in FLAT],
                    [cached_share([r]) for r in FLAT]) == 0.0
    state, detail = classify(FLAT)
    assert state == "flat-low-share"
    assert "low everywhere rather than low under load" in detail
    assert "cache-invalidated-by-changing-prefix" in handoff(state)


def test_a_share_that_climbs_with_load_is_not_scatter():
    state, detail = classify(RISING)
    assert state == "share-rises-with-load"
    assert "the opposite of scatter" in detail
    assert handoff(state) == ""


def test_hours_after_a_gap_are_excluded_before_correlating():
    # The exclusion, tested by the case it exists for. Leave the post-gap hours
    # in and the correlation goes sharply negative, because the cold hours are
    # also the ones that open a burst as it scales up. Take them out and the
    # linked hours are uniformly warm.
    everything = spearman([r["requests"] for r in BURSTY],
                          [cached_share([r]) for r in BURSTY])
    assert everything is not None and round(everything, 2) == -0.87

    assert len(continuation_rows(BURSTY)) == 42
    assert len(resumption_rows(BURSTY)) == 21
    assert cached_share(continuation_rows(BURSTY)) == 0.7
    assert cached_share(resumption_rows(BURSTY)) == 0.0

    state, detail = classify(BURSTY)
    assert state == "cold-only-after-idle"
    assert "70% cached in linked hours against 0% in the 21 hour(s)" in detail
    assert "prompt-cache-retention-left-at-default" in handoff(state)


def test_no_cached_tokens_at_any_load_is_an_eligibility_question():
    state, detail = classify(contiguous(lambda _load: 0.0))
    assert state == "no-cached-tokens"
    assert "not one cached" in detail
    assert "prompt-below-model-cache-minimum" in handoff(state)


def test_a_flat_request_rate_supports_no_verdict():
    # Nothing can be said about concurrency when the concurrency never moves,
    # and returning 0.0 there would read as "no relationship" rather than
    # "no evidence".
    steady = [hour(i, 1000, 0.6 if i % 2 else 0.2) for i in range(168)]
    assert spearman([r["requests"] for r in steady],
                    [cached_share([r]) for r in steady]) is None
    assert classify(steady)[0] == "load-does-not-vary"


def test_pooled_share_is_weighted_by_traffic():
    # An hour with nine requests must not outvote an hour with nine thousand.
    mixed = [hour(0, 10, 1.0), hour(1, 10_000, 0.1)]
    assert round(cached_share(mixed), 4) == 0.1009
    assert cached_share([]) is None
    assert cached_share([{"input": 0, "cached": 0}]) is None


def test_the_hour_index_survives_both_shapes_and_midnight():
    assert hour_index(1_755_388_800) == 1_755_388_800 // 3600
    assert hour_index("2026-08-17T23:00Z") + 1 == hour_index("2026-08-18T00:00Z")
    assert hour_label(hour_index("2026-08-17T09:00Z")) == "2026-08-17T09:00Z"
    assert hour_index("nonsense") is None
    assert hour_index(None) is None


def test_buckets_are_folded_into_project_and_model_series():
    buckets = [{"start_time": (BASE + i) * 3600,
                "results": [{"project_id": "proj_abc123", "model": "gpt-5.6",
                             "num_model_requests": LOAD[i % 24],
                             "input_tokens": LOAD[i % 24] * 2000,
                             "input_cached_tokens":
                                 int(LOAD[i % 24] * 2000
                                     * (0.72 - 0.00016 * LOAD[i % 24]))}]}
               for i in range(168)]
    series = rows_by_series(buckets)
    rows = series[("proj_abc123", "gpt-5.6")]
    assert len(rows) == 168
    assert [r["index"] for r in rows] == sorted(r["index"] for r in rows)
    assert classify(rows)[0] == "load-correlated-misses"


def test_thin_and_unreadable_windows_produce_no_verdict():
    assert classify([hour(i, 500, 0.5) for i in range(10)])[0] == "too-few-linked-hours"
    assert classify([])[0] == "too-few-linked-hours"
    assert classify(None)[0] == "too-few-linked-hours"
    assert spearman([1, 2], [1, 2]) is None
    assert spearman([1, 2, 3], None) is None
    assert load_split([]) == (None, None, None, None)
    assert rows_by_series([{"start_time": "bad", "results": []}]) == {}
