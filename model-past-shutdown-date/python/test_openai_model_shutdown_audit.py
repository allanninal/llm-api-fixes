import datetime as dt

from openai_model_shutdown_audit import parse_day, successor, verdict

TODAY = dt.date(2026, 8, 30)


def test_shutdown_date_is_read_as_a_plain_day():
    assert parse_day("2026-12-11") == dt.date(2026, 12, 11)
    assert parse_day("2026-12-11T00:00:00Z") == dt.date(2026, 12, 11)
    assert parse_day("") is None
    assert parse_day(None) is None
    assert parse_day("December 2026") is None


def test_a_date_already_passed_is_retired():
    state, detail = verdict({"id": "gpt-4-turbo", "shutdown_date": "2026-06-15"},
                            TODAY)
    assert state == "retired"
    assert "76 day(s) ago" in detail
    assert "misspelled" in detail


def test_a_shutdown_date_of_today_is_its_own_state():
    # The whole point of the note: this is happening now, not soon.
    state, detail = verdict({"id": "gpt-5-2025-08-07", "shutdown_date": "2026-08-30"},
                            TODAY)
    assert state == "retiring-today"
    assert "outage in progress" in detail


def test_a_future_date_belongs_to_the_other_note():
    state, detail = verdict({"id": "gpt-5-2025-08-07", "shutdown_date": "2026-12-11"},
                            TODAY)
    assert state == "scheduled"
    assert "103 day(s)" in detail


def test_no_shutdown_date_is_not_a_promise():
    state, detail = verdict({"id": "gpt-5.6-sol", "shutdown_date": None}, TODAY)
    assert state == "open"
    assert "not a guarantee" in detail
    assert verdict({"id": "gpt-5.6-sol"}, TODAY)[0] == "open"


def test_an_unreadable_date_is_not_silently_healthy():
    assert verdict({"id": "x", "shutdown_date": "soon"}, TODAY)[0] == "unreadable-date"
    assert verdict({"shutdown_date": "2026-01-01"}, TODAY)[0] == "unreadable"


def test_the_successor_is_family_level_and_admits_ignorance():
    assert successor("gpt-5-mini-2025-08-07") == "gpt-5.6-terra"
    assert successor("gpt-5-2025-08-07") == "gpt-5.6-sol"
    assert successor("dall-e-3") == "gpt-image-2"
    assert successor("some-vendor-model") is None
