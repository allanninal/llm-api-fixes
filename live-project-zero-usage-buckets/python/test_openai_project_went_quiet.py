from openai_project_went_quiet import (classify, complete_days, corroborate,
                                       daily, day_key, key_activity,
                                       surface_split)

DAYS = ["2026-08-%02d" % n for n in range(5, 19)]  # 14 complete days
NOW = 1787097600  # 2026-08-19T00:00:00Z


def test_a_project_that_stops_is_named_with_a_date_and_a_volume():
    # The note in one assertion. Twelve busy days, then two empty ones, and the
    # report has to say when it stopped and what it used to do.
    series = {day: 4102 for day in DAYS[:12]}
    state, detail = classify(series, DAYS)
    assert state == "went-quiet"
    assert "last traffic on 2026-08-16" in detail
    assert "2 complete day(s) ago" in detail
    assert "prior mean of 4102 request(s) a day" in detail

    # The project next to it never stopped, and must not be reported.
    assert classify({day: 4102 for day in DAYS}, DAYS)[0] == "live"


def test_a_launch_is_not_a_death_read_backwards():
    # The same shape, reversed. Get this wrong and the check fires on every new
    # project once and is muted by the end of the week.
    state, detail = classify({DAYS[12]: 900, DAYS[13]: 1200}, DAYS)
    assert state == "new-traffic"
    assert "first traffic in this window landed on 2026-08-17" in detail


def test_the_quiet_states_that_are_not_findings():
    assert classify({}, DAYS)[0] == "never-active"
    assert classify({DAYS[0]: 4}, DAYS)[0] == "too-little-traffic"
    assert classify({DAYS[0]: 4102}, DAYS[:2])[0] == "window-too-short"
    assert classify(None, DAYS)[0] == "never-active"


def test_today_is_never_in_the_axis():
    days = complete_days(NOW, 14)
    assert days == DAYS
    assert day_key(NOW) == "2026-08-19"
    assert day_key(NOW) not in days
    assert day_key("not an epoch") is None


def test_a_project_absent_from_a_bucket_is_a_zero_not_a_gap():
    # Buckets come back for the whole range; a project with no traffic is
    # simply not in the results. The day axis has to come from the window.
    buckets = [{"start_time": 1786579200,
                "results": [{"project_id": "proj_busy", "num_model_requests": 10}]},
               {"start_time": 1786665600, "results": []}]
    rows = daily(buckets)
    assert rows == {"proj_busy": {"2026-08-13": 10}}
    assert rows.get("proj_quiet") is None
    # Other surfaces count other things, and only one field is ever present.
    assert daily([{"start_time": 1786579200,
                   "results": [{"project_id": "p", "num_images": 7}]}]) \
        == {"p": {"2026-08-13": 7}}
    assert daily([]) == {}


def test_a_key_still_in_use_means_something_is_authenticating():
    keys = [{"last_used_at": NOW - 3600}, {"last_used_at": None},
            {"last_used_at": NOW - 900000}]
    used, since = key_activity(keys, NOW)
    assert used == NOW - 3600
    assert round(since, 2) == 0.04
    state, detail = corroborate(since)
    assert state == "key-still-used"
    assert "authenticating and not inferring" in detail


def test_a_key_frozen_with_the_buckets_means_the_integration_died():
    _, since = key_activity([{"last_used_at": NOW - 11 * 86400}], NOW)
    state, detail = corroborate(since)
    assert state == "key-quiet-too"
    assert "11.0 day(s) ago" in detail
    assert key_activity([], NOW) == (None, None)
    assert key_activity([{"last_used_at": "never"}], NOW) == (None, None)
    assert corroborate(None)[0] == "no-key-use"


def test_one_quiet_surface_beside_a_live_one_is_a_code_path():
    quiet, live = surface_split({"completions": "went-quiet",
                                 "embeddings": "live",
                                 "images": "never-active"})
    assert quiet == ["completions"]
    assert live == ["embeddings"]
    assert surface_split({}) == ([], [])
