from anthropic_itpm_headroom import (cache_read_share, cache_reads_count,
                                     chargeable_input, headroom_multiplier,
                                     itpm_by_group, limit_for, peaks, verdict)


def minute(stamp, model, uncached=0, write_5m=0, write_1h=0, read=0):
    """One 1m bucket from GET /v1/organizations/usage_report/messages."""
    return {"starting_at": stamp, "results": [{
        "model": model,
        "uncached_input_tokens": uncached,
        "cache_read_input_tokens": read,
        "cache_creation": {"ephemeral_5m_input_tokens": write_5m,
                           "ephemeral_1h_input_tokens": write_1h},
        "output_tokens": 12000,
    }]}


def test_a_full_input_limiter_with_no_cache_reads_is_the_finding():
    stats = peaks([minute("2026-08-30T14:%02d:00Z" % i, "claude-sonnet-5",
                          uncached=4_880_000 if i == 7 else 900_000,
                          read=100_000 if i == 7 else 0)
                   for i in range(20)])
    state, detail = verdict("claude-sonnet-5", stats["claude-sonnet-5"], 5_000_000)
    assert state == "itpm-saturated-uncached"
    assert "against an ITPM of 5000000 (98%)" in detail
    assert "cache reads were 2% of that minute's input" in detail
    assert "buys throughput and not only a discount" in detail
    # The throughput argument, which is the whole point of the note.
    assert round(headroom_multiplier(0.8), 1) == 5.0
    assert round(headroom_multiplier(0.0), 1) == 1.0


def test_the_same_full_ceiling_with_a_cached_prefix_is_a_different_finding():
    # 98% of ITPM again, but the prefix is already being read back. Telling this
    # reader to add a breakpoint sends them to do work that is already done.
    stats = peaks([minute("2026-08-30T14:%02d:00Z" % i, "claude-sonnet-5",
                          uncached=4_880_000 if i == 3 else 100_000,
                          read=19_520_000 if i == 3 else 0)
                   for i in range(20)])
    state, detail = verdict("claude-sonnet-5", stats["claude-sonnet-5"], 5_000_000)
    assert state == "itpm-saturated-already-cached"
    assert "cache reads were 80% of that minute's input" in detail
    assert "limit increase" in detail


def test_haiku_35_charges_cache_reads_so_caching_buys_no_headroom():
    assert cache_reads_count("claude-3-5-haiku-20241022") is True
    assert cache_reads_count("claude-haiku-4-5-20251001") is False
    assert cache_reads_count("claude-opus-5") is False
    # The read is inside the charged number on that model and outside it here.
    result = {"uncached_input_tokens": 1000, "cache_read_input_tokens": 4000,
              "cache_creation": {"ephemeral_5m_input_tokens": 500}}
    assert chargeable_input(result, "claude-sonnet-5") == 1500
    assert chargeable_input(result, "claude-3-5-haiku-20241022") == 5500

    stats = peaks([minute("2026-08-30T14:%02d:00Z" % i, "claude-3-5-haiku-20241022",
                          uncached=200_000, read=1_800_000) for i in range(20)])
    state, detail = verdict("claude-3-5-haiku-20241022",
                            stats["claude-3-5-haiku-20241022"], 2_000_000)
    assert state == "itpm-saturated-cache-counts"
    assert "buys no headroom at all" in detail


def test_chargeable_input_reads_the_nested_cache_creation_object():
    # The trap: these two fields live inside cache_creation, not at the top.
    assert chargeable_input({"uncached_input_tokens": 100,
                             "cache_creation": {"ephemeral_5m_input_tokens": 7,
                                                "ephemeral_1h_input_tokens": 3}},
                            "claude-opus-5") == 110
    assert chargeable_input({"cache_creation_input_tokens": 999}, "claude-opus-5") == 0
    assert chargeable_input({"uncached_input_tokens": None}, "claude-opus-5") == 0
    assert chargeable_input(None, "claude-opus-5") == 0


def test_the_peak_minute_survives_an_otherwise_quiet_window():
    # One saturated minute in twenty. The mean would read 24% of the ceiling and
    # report a comfortable workload that is 429ing every hour.
    stats = peaks([minute("2026-08-30T14:%02d:00Z" % i, "claude-opus-5",
                          uncached=4_800_000 if i == 11 else 200_000)
                   for i in range(20)])
    row = stats["claude-opus-5"]
    assert row["peak"] == 4_800_000
    assert row["peak_at"] == "2026-08-30T14:11:00Z"
    assert row["minutes"] == 20
    assert verdict("claude-opus-5", row, 5_000_000)[0] == "itpm-saturated-uncached"


def test_a_window_too_short_to_have_a_peak_gets_no_verdict():
    stats = peaks([minute("2026-08-30T14:00:00Z", "claude-opus-5", uncached=9_000_000)])
    assert verdict("claude-opus-5", stats["claude-opus-5"], 5_000_000)[0] == "too-few-buckets"


def test_an_unpublished_ceiling_is_not_an_absent_one():
    groups = itpm_by_group({"data": [
        {"model_group": "claude-sonnet-5",
         "limits": [{"type": "input_tokens_per_minute", "value": 5000000},
                    {"type": "output_tokens_per_minute", "value": 1000000}]},
        {"model_group": "claude-fable-5",
         "limits": [{"type": "output_tokens_per_minute", "value": 300000}]},
    ]})
    assert groups["claude-sonnet-5"] == 5000000
    assert groups["claude-fable-5"] is None
    assert limit_for(groups, "claude-sonnet-5-20260101") == 5000000
    assert limit_for(groups, "claude-fable-5") is None
    assert limit_for(groups, "claude-opus-5") is None
    assert verdict("claude-fable-5", {"minutes": 60, "peak": 9}, None)[0] == "no-limit-published"


def test_longest_prefix_wins_when_two_groups_could_claim_a_model():
    groups = {"claude-haiku": 1000, "claude-haiku-4-5": 5_000_000}
    assert limit_for(groups, "claude-haiku-4-5-20251001") == 5_000_000
    assert limit_for(groups, "") is None
    assert limit_for({}, "claude-opus-5") is None
    assert cache_read_share({"peak": 0, "peak_read": 0}, "claude-opus-5") is None
    assert headroom_multiplier(None) is None
