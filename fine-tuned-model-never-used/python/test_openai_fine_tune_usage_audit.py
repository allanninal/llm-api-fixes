import datetime as dt

from openai_fine_tune_usage_audit import base_model, days_until, verdict

NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
LIVE = {"gpt-4o-mini-2024-07-18", "gpt-5", "gpt-5-mini"}


def job(status="succeeded", model_id="ft:gpt-4o-mini-2024-07-18:acme::AbC123",
        base="gpt-4o-mini-2024-07-18", trained=4182900, **extra):
    body = {"id": "ftjob-test", "status": status, "fine_tuned_model": model_id,
            "model": base, "trained_tokens": trained}
    body.update(extra)
    return body


def test_trained_billed_and_never_called():
    state, detail = verdict(job(), 0, LIVE, NOW)
    assert state == "never-called"
    assert "0 request(s) in 30 days" in detail
    assert "4182900 trained token(s)" in detail


def test_a_model_serving_traffic_is_not_a_finding():
    assert verdict(job(), 91204, LIVE, NOW)[0] == "in-service"


def test_a_vanished_base_model_changes_both_answers():
    # Idle on a base that is going away: nothing to migrate, delete it.
    state, detail = verdict(job(base="gpt-4-0613",
                                model_id="ft:gpt-4-0613:acme::Old1"),
                            0, LIVE, NOW)
    assert state == "never-called-base-gone"
    assert "no longer listed" in detail
    assert "stop answering in 53 day(s)" in detail

    # In service on a base that is going away: this one is urgent.
    state, detail = verdict(job(base="gpt-4-0613",
                                model_id="ft:gpt-4-0613:acme::Old1"),
                            50000, LIVE, NOW)
    assert state == "in-service-base-gone"
    assert "going to stop" in detail


def test_jobs_that_produced_nothing_are_not_this_note():
    assert verdict(job(status="failed"), 0, LIVE, NOW)[0] == "not-succeeded"
    assert verdict(job(status="running"), 0, LIVE, NOW)[0] == "not-succeeded"
    assert verdict(job(status="cancelled"), 0, LIVE, NOW)[0] == "not-succeeded"
    state, detail = verdict(job(model_id=None), 0, LIVE, NOW)
    assert state == "unnamed"
    assert "by hand" in detail


def test_the_base_is_the_second_field_not_the_last_one():
    assert base_model("ft:gpt-4o-mini-2024-07-18:acme::AbC123") == "gpt-4o-mini-2024-07-18"
    # An optional suffix moves the trailing id along; the base does not move.
    assert base_model("ft:gpt-4o-2024-08-06:acme:nightly:AbC123") == "gpt-4o-2024-08-06"
    assert base_model("gpt-5") is None
    assert base_model("") is None
    assert base_model(None) is None


def test_the_deadline_is_floored_toward_the_past():
    assert days_until("2026-10-23", NOW) == 53
    # 12 hours short of the date is 0 days left, not 1.
    assert days_until("2026-08-31", NOW) == 0
    assert days_until("2026-08-30", NOW) == -1
    assert days_until("not-a-date", NOW) is None


def test_a_job_with_no_base_field_falls_back_to_the_model_id():
    # Some job objects carry the base only inside fine_tuned_model.
    state, _ = verdict({"id": "ftjob-x", "status": "succeeded",
                        "fine_tuned_model": "ft:gpt-4-0613:acme::Old1",
                        "trained_tokens": 100}, 0, LIVE, NOW)
    assert state == "never-called-base-gone"
