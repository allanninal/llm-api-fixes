from sora_shutdown_inventory import (REPLACEMENTS, SHUTDOWN, SORA_IDS,
                                     asset_deadline, days_left, iso_day,
                                     model_verdict, repair_lines,
                                     replacement_for, spend_verdict)

TODAY = "2026-08-31"


def test_there_is_no_successor_and_the_script_refuses_to_invent_one():
    # If somebody fills the table in with the closest-looking model id, this
    # fails and the message above it explains why that is not a kindness.
    assert REPLACEMENTS == {}
    for model_id in SORA_IDS:
        assert replacement_for(model_id) is None
    joined = " ".join(repair_lines("shutdown-dated"))
    assert "no successor model id" in joined
    assert "capability leaving the API" in joined
    assert "third-party provider or dropping the feature" in joined


def test_an_asset_that_expires_first_is_on_the_earlier_clock():
    state, deadline, detail = asset_deadline("2026-09-02", TODAY)
    assert state == "expires-first"
    assert deadline == "2026-09-02"
    assert "22 day(s) before the endpoint closes" in detail
    assert any("front of the queue" in line for line in repair_lines(state))


def test_an_asset_that_outlives_its_expiry_still_dies_with_the_endpoint():
    state, deadline, detail = asset_deadline("2026-12-01", TODAY)
    assert state == "outlives-the-endpoint"
    assert deadline == SHUTDOWN
    assert "the endpoint closes first" in detail

    # A null expiry is not an absence of a deadline. It inherits one.
    state, deadline, detail = asset_deadline(None, TODAY)
    assert state == "no-asset-expiry"
    assert deadline == SHUTDOWN
    assert "dies with the endpoint" in detail
    assert any("inherit" in line for line in repair_lines(state))


def test_an_expiry_already_past_means_the_bytes_are_gone():
    state, deadline, detail = asset_deadline("2026-08-04", TODAY)
    assert state == "already-expired"
    assert deadline == "2026-08-04"
    assert "already unreachable" in detail
    assert asset_deadline(TODAY, TODAY)[0] == "already-expired"


def test_unix_stamps_become_days_and_bad_ones_become_nothing():
    assert iso_day(1788000000) == "2026-08-29"
    assert iso_day(None) is None
    assert iso_day(0) is None
    assert iso_day("not a stamp") is None
    assert days_left(TODAY) == 24
    assert days_left("2026-10-01") == -7


def test_a_stated_shutdown_date_is_graded_apart_from_a_missing_one():
    state, detail = model_verdict("sora-2", 200, SHUTDOWN, TODAY)
    assert state == "shutdown-dated"
    assert "24 day(s) away" in detail

    state, detail = model_verdict("sora-2", 200, None, TODAY)
    assert state == "no-date-from-api"
    assert "published table is the only source" in detail

    assert model_verdict("sora-2", 404, None, TODAY)[0] == "already-gone"
    assert model_verdict("sora-2", 401, None, TODAY)[0] == "unreadable"
    assert model_verdict("sora-2", None, None, TODAY)[0] == "unreachable"
    assert model_verdict("sora-2", 200, "2026-08-01", TODAY)[0] == "past-shutdown"


def test_spend_is_a_proxy_and_says_so_in_the_case_that_looks_like_an_all_clear():
    state, total, detail = spend_verdict(
        [("Video generation", 400.5), ("sora-2-pro", 12.3), ("Text tokens", 99)], 30)
    assert state == "video-spend-accruing"
    assert round(total, 2) == 412.80
    assert "412.80" in detail

    state, total, detail = spend_verdict([("Text tokens", 99)], 30)
    assert state == "no-video-spend"
    assert total == 0.0
    assert "That is a proxy" in detail
    assert repair_lines(state) == []
