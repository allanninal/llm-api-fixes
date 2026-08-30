import pytest

from anthropic_prompt_cache_off import accumulate, cache_saving_ceiling, verdict


def test_accumulate_reads_the_nested_cache_creation_object():
    # The trap: these two fields live inside cache_creation, not at the top.
    total = accumulate([{
        "uncached_input_tokens": 100,
        "cache_read_input_tokens": 40,
        "cache_creation": {"ephemeral_5m_input_tokens": 7,
                           "ephemeral_1h_input_tokens": 3},
    }])
    assert total == {"uncached": 100, "cache_read": 40, "write_5m": 7, "write_1h": 3}


def test_accumulate_treats_absent_and_null_fields_as_zero():
    assert accumulate([{"uncached_input_tokens": None}])["uncached"] == 0
    assert accumulate([{}])["write_5m"] == 0
    assert accumulate(None)["cache_read"] == 0


def test_accumulate_adds_into_a_running_total():
    first = accumulate([{"uncached_input_tokens": 10}])
    second = accumulate([{"uncached_input_tokens": 5}], first)
    assert second["uncached"] == 15


def test_zero_reads_and_zero_writes_on_real_traffic_is_the_finding():
    state, detail = verdict({"uncached": 50_000_000, "cache_read": 0,
                             "write_5m": 0, "write_1h": 0})
    assert state == "never-used"
    assert "never been switched on" in detail


def test_writes_without_reads_is_the_other_note_not_this_one():
    state, detail = verdict({"uncached": 50_000_000, "cache_read": 0,
                             "write_5m": 4_000_000, "write_1h": 0})
    assert state == "writes-only"
    assert "worse" in detail or "more than leaving it off" in detail


def test_any_read_at_all_means_caching_is_on():
    assert verdict({"uncached": 5_000_000, "cache_read": 1, "write_5m": 0,
                    "write_1h": 0})[0] == "in-use"


def test_a_quiet_workload_makes_no_claim_either_way():
    state, _ = verdict({"uncached": 900, "cache_read": 0, "write_5m": 0, "write_1h": 0})
    assert state == "too-little-traffic"


def test_the_saving_ceiling_prices_the_reusable_share_at_the_read_rate():
    # 0.1x read rate, so 90% of the reusable share stops being paid for.
    assert cache_saving_ceiling(1_000_000, 1.0) == 900_000
    assert cache_saving_ceiling(1_000_000, 0.5) == 450_000
    assert cache_saving_ceiling(1_000_000, 0.0) == 0


def test_the_ceiling_refuses_a_fraction_that_is_not_a_fraction():
    with pytest.raises(ValueError):
        cache_saving_ceiling(1_000_000, 1.4)
