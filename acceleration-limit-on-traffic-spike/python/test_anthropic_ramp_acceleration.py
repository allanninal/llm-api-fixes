from anthropic_ramp_acceleration import (below_published_start, cache_creation,
                                         group_for_model, peak, ramp_factors,
                                         repair_lines, series, share,
                                         uncached_input, verdict)

LIMITS = {"requests_per_minute": 4_000,
          "input_tokens_per_minute": 10_000_000,
          "output_tokens_per_minute": 2_000_000}


def bucket(minute, model="claude-opus-5", uncached=0, out=0, read=0,
           create_5m=0, create_1h=0):
    return {"starting_at": "2026-08-31T09:%02d:00Z" % minute,
            "ending_at": "2026-08-31T09:%02d:00Z" % (minute + 1),
            "results": [{"model": model,
                         "uncached_input_tokens": uncached,
                         "output_tokens": out,
                         "cache_read_input_tokens": read,
                         "cache_creation": {
                             "ephemeral_5m_input_tokens": create_5m,
                             "ephemeral_1h_input_tokens": create_1h}}]}


def page(buckets):
    return [{"data": buckets, "has_more": False, "next_page": None}]


def test_a_steep_step_under_a_low_ceiling_is_the_finding():
    # The note. Four quiet minutes, one fifteenfold step, and a peak that never
    # gets past a fifth of the input limiter.
    rows = series(page([bucket(m, uncached=130_000, out=14_000) for m in range(4)]
                       + [bucket(4, uncached=1_940_000, out=140_000)]))
    state, detail, facts = verdict(rows["claude-opus-5"], LIMITS, "claude-opus-5")
    assert state == "acceleration-suspect"
    assert "step between adjacent minutes" in detail
    assert facts["peak_in"] == ("2026-08-31T09:04:00Z", 1_940_000.0)
    assert 0.19 < facts["in_share"] < 0.20
    assert round(facts["ramps"][0][2], 1) == 14.9
    assert any("ramp gradually" in line for line in repair_lines(state, facts))
    assert any("1 per second" in line for line in repair_lines(state, facts))


def test_the_same_ramp_against_a_saturated_limiter_is_the_other_note():
    # The guard. Without this the note fires on every busy workload and takes
    # the credit for a finding that belongs to the output limiter note.
    rows = series(page([bucket(m, out=120_000) for m in range(4)]
                       + [bucket(4, out=1_870_000)]))
    state, detail, _ = verdict(rows["claude-opus-5"], LIMITS, "claude-opus-5")
    assert state == "limiter-saturated"
    assert "output limiter note, not this one" in detail
    assert any("really is the headline number" in line
               for line in repair_lines(state))


def test_input_is_summed_the_way_the_limiter_counts_it():
    result = {"uncached_input_tokens": 1_000, "cache_read_input_tokens": 900_000,
              "cache_creation": {"ephemeral_5m_input_tokens": 400,
                                 "ephemeral_1h_input_tokens": 600}}
    assert cache_creation(result) == 1_000.0
    # 900,000 cache reads are excluded: they do not count toward ITPM.
    assert uncached_input(result) == 2_000.0
    assert uncached_input({}) == 0.0 and uncached_input(None) == 0.0
    rows = series(page([bucket(0, uncached=1_000, read=900_000, create_5m=400,
                               create_1h=600)]))
    assert rows["claude-opus-5"][0][1] == 2_000.0
    assert rows["claude-opus-5"][0][3] == 900_000.0


def test_a_ramp_off_a_trivial_base_is_not_a_ramp():
    rows = [("09:00", 12.0, 0.0, 0.0), ("09:01", 900.0, 0.0, 0.0)]
    assert ramp_factors(rows, 1) == []
    big = [("09:00", 100_000.0, 0.0, 0.0), ("09:01", 400_000.0, 0.0, 0.0),
           ("09:02", 200_000.0, 0.0, 0.0)]
    factors = ramp_factors(big, 1)
    assert len(factors) == 1 and factors[0][2] == 4.0
    assert peak(big, 1) == ("09:01", 400_000.0)
    assert peak([], 1) == ("", 0.0)
    assert share(10, 0) is None and share(10, None) is None


def test_a_model_resolves_to_its_group_by_exact_membership():
    groups = [{"group_type": "model_group",
               "models": ["claude-opus-4-5", "claude-opus-4-8"],
               "limits": [{"type": "input_tokens_per_minute", "value": 10_000_000}]},
              {"group_type": "batch", "models": None,
               "limits": [{"type": "enqueued_batch_requests", "value": 500_000}]}]
    assert group_for_model(groups, "claude-opus-4-8") == {
        "input_tokens_per_minute": 10_000_000.0}
    # A prefix match would hand claude-opus-5 the 4.x group's numbers, which is
    # a different bucket entirely.
    assert group_for_model(groups, "claude-opus-5") == {}
    assert group_for_model(None, "claude-opus-5") == {}


def test_configured_limits_under_the_published_start_tier_are_reported():
    assert below_published_start("claude-fable-5",
                                 {"input_tokens_per_minute": 250_000}) == [
        ("input_tokens_per_minute", 250_000, 500_000)]
    assert below_published_start("claude-opus-5",
                                 {"input_tokens_per_minute": 10_000_000}) == []
    assert below_published_start("claude-opus-5", {}) == []
    assert any("Evaluation tier" in line
               for line in repair_lines("below-published-start"))


def test_an_empty_window_is_not_a_finding():
    state, detail, _ = verdict([], LIMITS, "claude-opus-5")
    assert state == "no-traffic" and "no usage" in detail
    assert verdict(None, None, None)[0] == "no-traffic"
    assert series(None) == {} and repair_lines("steady") == []
    steady = series(page([bucket(m, uncached=100_000) for m in range(3)]))
    assert verdict(steady["claude-opus-5"], LIMITS, "claude-opus-5")[0] == "steady"
