from anthropic_cache_write_ratio import (
    accumulate, break_even_ratio, effective_multiplier, verdict,
)


def test_accumulate_keeps_the_two_ttls_apart():
    # Summing them would destroy the information break-even needs.
    total = accumulate([{
        "cache_read_input_tokens": 5,
        "cache_creation": {"ephemeral_5m_input_tokens": 100,
                           "ephemeral_1h_input_tokens": 20},
    }])
    assert total["write_5m"] == 100
    assert total["write_1h"] == 20
    assert total["cache_read"] == 5


def test_break_even_for_pure_5m_writes():
    # (1.25 - 1) / (1 - 0.1)
    assert round(break_even_ratio(1000, 0), 4) == 0.2778


def test_break_even_for_pure_1h_writes_is_about_four_times_higher():
    # (2.0 - 1) / (1 - 0.1)
    assert round(break_even_ratio(0, 1000), 4) == 1.1111


def test_break_even_of_nothing_written_is_none_not_zero():
    assert break_even_ratio(0, 0) is None


def test_at_break_even_the_effective_multiplier_is_exactly_one():
    # The identity that keeps the two functions from drifting apart.
    for w5, w1h in ((1000, 0), (0, 1000), (600, 400)):
        reads = break_even_ratio(w5, w1h) * (w5 + w1h)
        assert round(effective_multiplier(w5, w1h, reads), 6) == 1.0


def test_writes_with_no_reads_cost_more_than_not_caching():
    assert effective_multiplier(1000, 0, 0) == 1.25
    assert effective_multiplier(0, 1000, 0) == 2.0


def test_a_key_that_writes_and_never_reads_is_losing():
    state, detail = verdict({"cache_read": 0, "write_5m": 5_000_000, "write_1h": 0})
    assert state == "losing"
    assert "1.25x" in detail


def test_a_key_reading_back_many_times_is_paying_off():
    state, _ = verdict({"cache_read": 50_000_000, "write_5m": 5_000_000, "write_1h": 0})
    assert state == "paying-off"


def test_just_above_break_even_is_marginal_not_safe():
    writes = 5_000_000
    reads = int(break_even_ratio(writes, 0) * writes * 1.1)
    assert verdict({"cache_read": reads, "write_5m": writes, "write_1h": 0})[0] == "marginal"


def test_no_writes_and_no_reads_is_the_other_note():
    state, detail = verdict({"cache_read": 0, "write_5m": 0, "write_1h": 0})
    assert state == "no-caching"
    assert "different problem" in detail


def test_reads_with_no_writes_in_the_window_is_not_a_ratio():
    state, detail = verdict({"cache_read": 9_000_000, "write_5m": 0, "write_1h": 0})
    assert state == "reads-only"
    assert "Widen the window" in detail


def test_a_trickle_of_writes_makes_no_claim():
    assert verdict({"cache_read": 0, "write_5m": 10, "write_1h": 0})[0] == "too-little-traffic"
