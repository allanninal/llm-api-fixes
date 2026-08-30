from anthropic_cache_prefix_churn import (churn_runs, classify, gap_profile,
                                         handoff, minute_index, minute_key,
                                         rows_by_key, totals, ttl_split,
                                         write_share, writes)

BASE = minute_index("2026-08-31T10:00Z")


def minute(offset, uncached=100_000, write5m=0, write1h=0, reads=0):
    index = BASE + offset
    hour, rest = divmod(offset, 60)
    return {"minute": "2026-08-31T%02d:%02dZ" % (10 + hour, rest), "index": index,
            "uncached": uncached, "write5m": write5m, "write1h": write1h,
            "reads": reads}


# Every call writes: a hundred and twenty adjacent minutes, never a read.
CHURN = [minute(i, write5m=500_000) for i in range(120)]
# Byte-identical totals, six writing minutes twenty apart. Traffic slower than
# the TTL, which is a different note.
SLOW = [minute(i, write5m=10_000_000 if i % 20 == 0 else 0) for i in range(120)]


def test_a_write_in_every_adjacent_minute_and_never_a_read():
    # The note in one assertion. The run is longer than the TTL, so the entry
    # written at 10:00 was alive at 10:04 and the call at 10:04 wrote another.
    sums = totals(CHURN)
    assert sums["writes"] == 60_000_000 and sums["uncached"] == 12_000_000
    assert sums["reads"] == 0 and sums["active"] == 120
    assert round(write_share(CHURN[0]), 4) == 0.8333

    runs = churn_runs(CHURN)
    assert len(runs) == 1 and len(runs[0]) == 120

    state, detail = classify(CHURN)
    assert state == "prefix-churn"
    assert "longest run 120 adjacent minute(s)" in detail
    assert "from 2026-08-31T10:00Z to 2026-08-31T11:59Z" in detail
    assert ttl_split(sums)[0] == "5m-dominant"


def test_identical_totals_spaced_out_are_a_different_note():
    # The pair. Same writes, same uncached input, same zero reads, and the
    # opposite conclusion. Nothing an hourly bucket can see separates these.
    assert totals(SLOW)["writes"] == totals(CHURN)["writes"]
    assert totals(SLOW)["uncached"] == totals(CHURN)["uncached"]
    assert totals(SLOW)["reads"] == totals(CHURN)["reads"] == 0

    assert max(len(r) for r in churn_runs(SLOW)) == 1
    assert gap_profile(SLOW) == 20.0
    assert gap_profile(CHURN) == 1.0

    state, detail = classify(SLOW)
    assert state == "gap-driven-misses"
    assert "median of 20 minute(s) apart" in detail
    assert "cache-writes-with-no-reads" in handoff(state)


def test_reads_anywhere_hand_the_finding_to_the_ratio_note():
    warm = [minute(i, write5m=500_000 if i == 0 else 0,
                   reads=400_000 if i else 0) for i in range(120)]
    state, detail = classify(warm)
    assert state == "cache-is-read"
    assert "against 500000 written" in detail
    assert "write-to-read ratio" in handoff(state)


def test_no_writes_and_no_reads_is_the_never_switched_on_note():
    off = [minute(i) for i in range(120)]
    state, detail = classify(off)
    assert state == "caching-off"
    assert "no cache writes and no cache reads" in detail
    assert "prompt-caching-never-used" in handoff(state)
    assert ttl_split(totals(off))[0] == "no-writes"
    reads_only = [minute(i, reads=400_000) for i in range(120)]
    assert classify(reads_only)[0] == "reads-only"


def test_a_minority_cached_fragment_is_not_the_prefix():
    small = [minute(i, uncached=900_000, write5m=100_000) for i in range(120)]
    state, detail = classify(small)
    assert state == "small-cached-prefix"
    assert "writes are 10% of input" in detail
    assert handoff(state) == ""


def test_an_hour_long_ttl_makes_the_same_run_worse():
    hourly = [minute(i, write1h=500_000) for i in range(120)]
    state, _ = classify(hourly)
    assert state == "prefix-churn"
    ttl_state, ttl_detail = ttl_split(totals(hourly))
    assert ttl_state == "1h-dominant"
    assert "2x base input" in ttl_detail
    assert ttl_split({"write5m": 10, "write1h": 10})[0] == "mixed"


def test_a_run_crossing_an_hour_boundary_is_not_broken_in_half():
    # 10:57 through 11:02. Comparing the minute strings puts 10:59 and 11:00
    # sixty apart and reports two runs of three.
    crossing = [minute(i, write5m=500_000) for i in range(57, 63)]
    assert [r["minute"] for r in crossing][:4] == [
        "2026-08-31T10:57Z", "2026-08-31T10:58Z", "2026-08-31T10:59Z",
        "2026-08-31T11:00Z"]
    runs = churn_runs(crossing)
    assert len(runs) == 1 and len(runs[0]) == 6
    assert minute_index("2026-08-31T11:00Z") - minute_index("2026-08-31T10:59Z") == 1


def test_the_nested_cache_creation_object_is_actually_read():
    buckets = [{"starting_at": "2026-08-31T10:0%dZ" % i,
                "results": [{"api_key_id": "apikey_01Ab", "model": "claude-opus-5",
                             "uncached_input_tokens": 100_000,
                             "cache_read_input_tokens": 0,
                             "cache_creation": {"ephemeral_5m_input_tokens": 500_000,
                                                "ephemeral_1h_input_tokens": 0}}]}
               for i in range(6)]
    series = rows_by_key(buckets)
    rows = series[("apikey_01Ab", "claude-opus-5")]
    assert len(rows) == 6
    assert writes(rows[0]) == 500_000
    assert [r["index"] for r in rows] == sorted(r["index"] for r in rows)
    state, _ = classify(rows, min_active=6)
    assert state == "prefix-churn"


def test_thin_and_unreadable_windows_produce_no_verdict():
    assert classify([minute(i, write5m=500_000) for i in range(4)])[0] == "too-little-traffic"
    assert classify([])[0] == "too-little-traffic"
    assert classify(None)[0] == "too-little-traffic"
    assert write_share({"uncached": 0, "write5m": 0, "write1h": 0}) is None
    assert gap_profile([]) is None
    assert minute_key("nonsense") is None
    assert minute_index(None) is None
    assert rows_by_key([{"starting_at": "bad", "results": []}]) == {}
