from openai_cache_cold_after_idle import (bin_shares, cached_share, classify,
                                          collapse_bin, foregone_tokens,
                                          gap_bin, handoff, hour_index,
                                          hour_label, repair_lines,
                                          rows_by_series, with_gaps)

BASE = hour_index("2026-08-17T00:00Z")


def hour(offset, share, requests=800):
    tokens = requests * 2000
    return {"index": BASE + offset, "hour": hour_label(BASE + offset),
            "requests": requests, "input": tokens,
            "cached": int(round(tokens * share))}


def nightly(resume_share=0.0, warm_share=0.75):
    """A batch that runs 02:00 to 05:00 and then sleeps for twenty-one hours.

    Every hour sends the same number of requests, so nothing about load can
    explain the difference between them.
    """
    rows = []
    for day in range(14):
        for step in range(3):
            rows.append(hour(day * 24 + 2 + step,
                             resume_share if step == 0 else warm_share))
    return rows


def two_on_one_off(resume_share=0.0, warm_share=0.70):
    """Two hours on, one hour off, for a fortnight. Every gap is a single hour."""
    rows = []
    for pair in range(112):
        rows.append(hour(pair * 3, resume_share))
        rows.append(hour(pair * 3 + 1, warm_share))
    return rows


NIGHTLY = nightly()
HOURLY_GAPS = two_on_one_off()
CONTINUOUS = [hour(i, 0.60) for i in range(336)]


def test_the_share_against_gap_length_is_the_finding():
    # The note in one assertion. Same prefix, same request rate, and the only
    # thing that separates a cold hour from a warm one is what happened in the
    # twenty-one hours before it.
    annotated = with_gaps(NIGHTLY)
    assert len(annotated) == 41
    assert {r["requests"] for r in annotated} == {800}

    bands = bin_shares(annotated)
    assert bands["continuous"]["hours"] == 28
    assert bands["continuous"]["share"] == 0.75
    assert bands["6-23h"]["hours"] == 13
    assert bands["6-23h"]["share"] == 0.0
    assert collapse_bin(bands) == "6-23h"

    state, detail = classify(NIGHTLY)
    assert state == "cold-after-idle"
    assert "75% cached in continuously busy hours" in detail
    assert "0% in the 13 hour(s) that resume after a gap of 6-23h" in detail
    assert handoff(state) == ""


def test_the_shortest_collapsed_band_is_the_one_reported():
    # Shortest, not worst. A single idle hour losing the entry is a retention
    # default; a day losing it is a schedule, and the repair differs.
    bands = bin_shares(with_gaps(HOURLY_GAPS))
    assert bands["1h"]["hours"] == 111
    assert bands["1h"]["share"] == 0.0
    assert collapse_bin(bands) == "1h"

    state, detail = classify(HOURLY_GAPS)
    assert state == "cold-after-idle"
    assert "gap of 1h" in detail
    assert "a single idle hour is already enough" in repair_lines("1h", 0)[0]
    assert "24h retention option" in repair_lines("24h+", 0)[0]


def test_a_series_with_no_gaps_is_someone_elses_note():
    state, detail = classify(CONTINUOUS)
    assert state == "never-idle"
    assert "only 0 of them resume after a gap" in detail
    assert "prompt-cache-key-not-set" in handoff(state)


def test_cold_in_the_busy_hours_too_is_not_eviction():
    # If the entry is cold when it is certainly still alive, the gap is not
    # what is losing it.
    state, detail = classify(nightly(resume_share=0.0, warm_share=0.0))
    assert state == "cold-everywhere"
    assert "0% cached even in continuously busy hours" in detail
    assert "cache-invalidated-by-changing-prefix" in handoff(state)
    assert "prompt-below-model-cache-minimum" in handoff(state)


def test_a_weak_warm_baseline_refuses_the_finding():
    state, detail = classify(nightly(resume_share=0.0, warm_share=0.10))
    assert state == "warm-baseline-too-weak"
    assert "barely caching at the best of times" in detail


def test_a_batch_that_resumes_warm_is_not_a_finding():
    state, detail = classify(nightly(resume_share=0.55))
    assert state == "warm-after-idle"
    assert "no gap band has collapsed" in detail


def test_the_first_hour_of_the_window_is_dropped_not_guessed():
    # Nothing is visible before the window starts, so the first row's gap is
    # unknowable and counting it as continuous would flatter the baseline.
    rows = [hour(0, 0.0), hour(1, 0.9), hour(9, 0.0), hour(10, 0.9)]
    annotated = with_gaps(rows)
    assert [r["gap"] for r in annotated] == [0, 7, 0]
    assert len(annotated) == len(rows) - 1
    assert with_gaps([hour(0, 0.5)]) == []
    assert with_gaps([]) == []


def test_the_gap_bands_line_up_with_the_repairs():
    assert gap_bin(0) == "continuous"
    assert gap_bin(1) == "1h"
    assert gap_bin(2) == "2-5h" and gap_bin(5) == "2-5h"
    assert gap_bin(6) == "6-23h" and gap_bin(23) == "6-23h"
    assert gap_bin(24) == "24h+" and gap_bin(500) == "24h+"
    # A band with almost no hours in it cannot decide anything.
    thin = {"1h": {"hours": 1, "input": 100, "cached": 0, "share": 0.0},
            "24h+": {"hours": 40, "input": 100, "cached": 0, "share": 0.0}}
    assert collapse_bin(thin) == "24h+"


def test_the_foregone_tokens_are_priced_at_the_workloads_own_warm_rate():
    bands = bin_shares(with_gaps(NIGHTLY))
    # 13 resumption hours of 1.6M input tokens, 75% of which would have been
    # cached had the entry survived the night.
    assert bands["6-23h"]["input"] == 13 * 800 * 2000
    assert foregone_tokens(bands, 0.75) == 15_600_000
    assert foregone_tokens(bands, None) == 0
    assert foregone_tokens({}, 0.75) == 0


def test_buckets_are_folded_and_idle_hours_never_become_rows():
    buckets = [{"start_time": (BASE + day * 24 + 2 + step) * 3600,
                "results": [{"project_id": "proj_abc123", "model": "gpt-5.6",
                             "num_model_requests": 800,
                             "input_tokens": 1_600_000,
                             "input_cached_tokens": 0 if step == 0 else 1_200_000}]}
               for day in range(14) for step in range(3)]
    series = rows_by_series(buckets)
    rows = series[("proj_abc123", "gpt-5.6")]
    assert len(rows) == 42
    assert cached_share(rows) == 0.5
    assert classify(rows)[0] == "cold-after-idle"


def test_thin_and_unreadable_windows_produce_no_verdict():
    assert classify([hour(i, 0.5) for i in range(10)])[0] == "too-few-hours"
    assert classify([])[0] == "too-few-hours"
    assert classify(None)[0] == "too-few-hours"
    assert cached_share([]) is None
    assert bin_shares([]) == {}
    assert collapse_bin({}) is None
    assert hour_index("nonsense") is None
    assert rows_by_series([{"start_time": "bad", "results": []}]) == {}
