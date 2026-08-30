import datetime as dt

from anthropic_alias_pinning_audit import parse_created, verdict

TODAY = dt.date(2026, 8, 30)


def model(model_id, created="2025-09-29T00:00:00Z"):
    return {"id": model_id, "created_at": created, "type": "model"}


def test_a_string_that_resolves_to_something_else_is_an_alias():
    state, detail = verdict("claude-sonnet-4-5",
                            model("claude-sonnet-4-5-20250929"), TODAY)
    assert state == "alias"
    assert "resolves to claude-sonnet-4-5-20250929" in detail
    assert "Pin claude-sonnet-4-5-20250929" in detail


def test_a_dated_id_that_resolves_to_itself_is_pinned():
    state, detail = verdict("claude-haiku-4-5-20251001",
                            model("claude-haiku-4-5-20251001"), TODAY)
    assert state == "pinned"
    assert "resolves to itself" in detail


def test_a_dateless_id_that_resolves_to_itself_is_also_pinned():
    # The trap: appending a date to a 4.6-or-later id gives a 404, so the check
    # has to read the resolution rather than look for a date suffix.
    state, detail = verdict("claude-opus-4-8", model("claude-opus-4-8"), TODAY)
    assert state == "pinned-dateless"
    assert "Do not append a date" in detail


def test_a_404_says_what_probably_caused_it():
    state, detail = verdict("claude-opus-4-8-20260601", None, TODAY)
    assert state == "not-found"
    assert "remove it" in detail


def test_the_age_of_the_resolved_snapshot_is_measured_from_the_date_passed_in():
    assert parse_created("2025-09-29T00:00:00Z") == dt.date(2025, 9, 29)
    assert parse_created("") is None
    assert parse_created("last autumn") is None
    detail = verdict("claude-sonnet-4-5", model("claude-sonnet-4-5-20250929"),
                     TODAY)[1]
    assert "335 day(s) ago" in detail


def test_a_missing_created_at_drops_the_age_rather_than_inventing_one():
    state, detail = verdict("claude-sonnet-4-5",
                            {"id": "claude-sonnet-4-5-20250929"}, TODAY)
    assert state == "alias"
    assert "day(s) ago" not in detail


def test_an_empty_string_or_a_headless_object_is_unreadable():
    assert verdict("", model("x"), TODAY)[0] == "unreadable"
    assert verdict("claude-opus-4-8", {"created_at": "x"}, TODAY)[0] == "unreadable"
