from anthropic_otpm_ceiling import (generated, implied_mean_output, limits_by_group,
                                    limits_for, output_to_input_ratio, peaks,
                                    received, verdict)

SONNET = {"requests_per_minute": 4000,
          "input_tokens_per_minute": 5000000,
          "output_tokens_per_minute": 1000000}


def minute(stamp, model, out=0, uncached=0, read=0):
    """One 1m bucket from GET /v1/organizations/usage_report/messages."""
    return {"starting_at": stamp, "results": [{
        "model": model,
        "output_tokens": out,
        "uncached_input_tokens": uncached,
        "cache_read_input_tokens": read,
        "cache_creation": {"ephemeral_5m_input_tokens": 0,
                           "ephemeral_1h_input_tokens": 0},
    }]}


def test_a_full_output_limiter_beside_a_comfortable_input_one_is_the_finding():
    stats = peaks([minute("2026-08-30T14:%02d:00Z" % i, "claude-opus-5",
                          out=980_000 if i == 5 else 200_000,
                          uncached=1_200_000 if i == 5 else 400_000)
                   for i in range(20)])
    state, detail = verdict("claude-opus-5", stats["claude-opus-5"], SONNET)
    assert state == "otpm-saturated"
    assert "generated 980000 of an OTPM of 1000000 (98%)" in detail
    assert "while input sat at 24% of ITPM" in detail
    assert "no cached output" in detail
    # The conclusion the note exists for: RPM was never the ceiling.
    assert round(implied_mean_output(980_000, 4000)) == 245
    assert round(output_to_input_ratio(SONNET) * 100) == 20


def test_a_full_input_limiter_is_handed_to_the_other_note():
    # The same fold and the same endpoints, and the opposite finding. If this
    # state did not exist, this script would prescribe batching and effort
    # changes for a workload whose repair is a cache breakpoint.
    stats = peaks([minute("2026-08-30T14:%02d:00Z" % i, "claude-sonnet-5",
                          out=100_000 if i == 9 else 20_000,
                          uncached=4_900_000 if i == 9 else 300_000)
                   for i in range(20)])
    state, detail = verdict("claude-sonnet-5", stats["claude-sonnet-5"], SONNET)
    assert state == "input-bound"
    assert "input limiter is the one that is full here" in detail


def test_both_limiters_full_is_volume_rather_than_shape():
    stats = peaks([minute("2026-08-30T14:%02d:00Z" % i, "claude-sonnet-5",
                          out=950_000, uncached=4_800_000) for i in range(20)])
    state, detail = verdict("claude-sonnet-5", stats["claude-sonnet-5"], SONNET)
    assert state == "both-limiters-saturated"
    assert "does nothing for the output side" in detail


def test_the_input_recorded_is_from_the_minute_output_peaked():
    # Output peaks at 14:05 and input peaks at 14:12. Taking the maximum of each
    # independently would report 98% of OTPM against 98% of ITPM and invent a
    # minute that never happened.
    buckets = [minute("2026-08-30T14:%02d:00Z" % i, "claude-opus-5",
                      out=200_000, uncached=400_000) for i in range(20)]
    buckets[5] = minute("2026-08-30T14:05:00Z", "claude-opus-5",
                        out=980_000, uncached=1_200_000)
    buckets[12] = minute("2026-08-30T14:12:00Z", "claude-opus-5",
                         out=300_000, uncached=4_900_000)
    row = peaks(buckets)["claude-opus-5"]
    assert row["peak_out"] == 980_000
    assert row["peak_at"] == "2026-08-30T14:05:00Z"
    assert row["input_at_peak"] == 1_200_000
    assert verdict("claude-opus-5", row, SONNET)[0] == "otpm-saturated"


def test_input_is_summed_from_every_field_that_carries_it():
    result = {"output_tokens": 50, "uncached_input_tokens": 100,
              "cache_read_input_tokens": 900,
              "cache_creation": {"ephemeral_5m_input_tokens": 7,
                                 "ephemeral_1h_input_tokens": 3}}
    assert generated(result) == 50
    assert received(result) == 1010
    assert generated({}) == 0
    assert generated(None) == 0
    assert received(None) == 0


def test_the_implied_answer_length_refuses_to_guess():
    assert implied_mean_output(980_000, None) is None
    assert implied_mean_output(980_000, 0) is None
    assert implied_mean_output(0, 4000) is None
    assert output_to_input_ratio({"output_tokens_per_minute": 1000}) is None
    assert output_to_input_ratio(None) is None


def test_an_unpublished_output_ceiling_gets_no_verdict():
    groups = limits_by_group({"data": [
        {"model_group": "claude-sonnet-5", "limits": [
            {"type": "requests_per_minute", "value": 4000},
            {"type": "input_tokens_per_minute", "value": 5000000},
            {"type": "output_tokens_per_minute", "value": 1000000}]},
        {"model_group": "claude-fable-5", "limits": [
            {"type": "requests_per_minute", "value": 500}]},
    ]})
    assert limits_for(groups, "claude-sonnet-5-20260101") == SONNET
    fable = limits_for(groups, "claude-fable-5")
    assert fable["output_tokens_per_minute"] is None
    assert verdict("claude-fable-5", {"minutes": 60, "peak_out": 9}, fable)[0] \
        == "no-limit-published"
    assert limits_for(groups, "claude-haiku-4-5-20251001") is None
    assert verdict("claude-opus-5", {"minutes": 2, "peak_out": 9}, SONNET)[0] \
        == "too-few-buckets"
