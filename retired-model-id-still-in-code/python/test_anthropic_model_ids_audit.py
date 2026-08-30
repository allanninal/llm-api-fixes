import datetime as dt

from anthropic_model_ids_audit import days_since, replacement, verdict

TODAY = dt.date(2026, 8, 30)
LIVE = {"claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
        "claude-opus-4-1-20250805"}


def test_an_id_in_the_live_list_is_callable():
    state, detail = verdict("claude-sonnet-4-6", LIVE, TODAY)
    assert state == "live"
    assert "live models list" in detail


def test_an_id_missing_from_the_list_and_on_the_table_is_retired():
    state, detail = verdict("claude-3-5-sonnet-20241022", LIVE - {"x"}, TODAY)
    assert state == "retired"
    assert "2025-10-28" in detail
    assert "not_found_error" in detail
    assert "claude-sonnet-4-6" in detail


def test_the_days_since_retirement_are_counted_from_the_date_passed_in():
    assert days_since("2026-06-15", TODAY) == 76
    assert days_since("not a date", TODAY) is None
    assert "76 day(s) ago" in verdict("claude-opus-4-20250514", set(), TODAY)[1]


def test_missing_from_the_list_but_not_on_the_table_is_unknown():
    state, detail = verdict("claude-sonnet-4-6-20260101", set(), TODAY)
    assert state == "unknown"
    assert "Bedrock" in detail


def test_the_api_wins_over_the_hardcoded_table():
    # The table is a copy of a web page and this one has gone stale. Reporting
    # an outage on a model the API is still serving would be worse than useless.
    state, detail = verdict("claude-opus-4-1-20250805", LIVE, TODAY)
    assert state == "table-stale"
    assert "Trust the API" in detail


def test_an_empty_string_is_not_silently_live():
    assert verdict("", LIVE, TODAY)[0] == "unreadable"
    assert verdict(None, LIVE, TODAY)[0] == "unreadable"


def test_the_replacement_is_family_level_and_admits_ignorance():
    assert replacement("claude-3-opus-20240229") == "claude-opus-4-8"
    assert replacement("claude-3-5-haiku-20241022") == "claude-haiku-4-5-20251001"
    assert replacement("claude-instant-1.2") == "claude-haiku-4-5-20251001"
    assert replacement("claude-2.1") == "claude-sonnet-4-6"
    assert replacement("some-other-vendor-model") is None
