import datetime as dt

from openai_model_retirement_window import plan, traffic_note

TODAY = dt.date(2026, 8, 30)


def dated(day, model_id="gpt-5-2025-08-07"):
    return {"id": model_id, "shutdown_date": day}


def test_a_date_inside_the_window_is_due():
    state, detail = plan(dated("2026-11-15"), TODAY)
    assert state == "due"
    assert "77 day(s) left" in detail


def test_a_date_under_a_month_out_is_urgent_not_merely_due():
    state, detail = plan(dated("2026-09-20"), TODAY)
    assert state == "urgent"
    assert "not next cycle" in detail


def test_a_date_beyond_the_window_is_left_alone():
    assert plan(dated("2027-06-01"), TODAY)[0] == "later"


def test_the_window_and_the_urgency_line_are_both_arguments():
    model = dated("2026-11-15")
    assert plan(model, TODAY)[0] == "due"
    assert plan(model, TODAY, window_days=30)[0] == "later"
    assert plan(model, TODAY, window_days=90, urgent_within=120)[0] == "urgent"


def test_a_date_already_passed_is_out_of_scope_for_planning():
    state, detail = plan(dated("2026-07-01"), TODAY)
    assert state == "expired"
    assert "already failing" in detail


def test_no_date_is_unscheduled_rather_than_safe():
    state, detail = plan({"id": "gpt-5.6-sol"}, TODAY)
    assert state == "unscheduled"
    assert "Re-read" in detail
    assert plan({"id": "x", "shutdown_date": "Q4"}, TODAY)[0] == "unreadable-date"


def test_unmeasured_traffic_and_zero_traffic_do_not_read_the_same():
    assert "no admin key" in traffic_note(None)
    assert "config file" in traffic_note(0)
    assert "4000000 request(s)" in traffic_note(4000000)
    assert "config file" in plan(dated("2026-09-20"), TODAY, requests_30d=0)[1]
    assert "no admin key" in plan(dated("2026-09-20"), TODAY)[1]
