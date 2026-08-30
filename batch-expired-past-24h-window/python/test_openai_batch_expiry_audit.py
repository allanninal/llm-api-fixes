from openai_batch_expiry_audit import counts_of, deadline, verdict

# 2026-08-30T00:00:00Z. Fixed, because every state here is a subtraction from it.
NOW = 1788048000
HOUR = 3600


def batch(status="in_progress", total=20000, completed=8000, **extra):
    body = {"id": "batch_test", "status": status,
            "request_counts": {"total": total, "completed": completed,
                               "failed": 0}}
    body.update(extra)
    return body


def test_an_expired_batch_reports_the_rows_that_never_ran():
    state, detail = verdict(
        batch(status="expired", total=50000, completed=20000,
              expired_at=NOW - HOUR), NOW)
    assert state == "expired"
    assert "30000 row(s) unfinished" in detail
    assert "batch_expired" in detail


def test_a_batch_close_to_its_deadline_is_the_useful_finding():
    state, detail = verdict(batch(expires_at=NOW + 2 * HOUR), NOW, warn_hours=4)
    assert state == "expiring-soon"
    assert "2.0 hour(s) of window left" in detail
    assert "second batch" in detail


def test_a_batch_with_room_left_is_left_alone():
    state, detail = verdict(batch(expires_at=NOW + 23 * HOUR), NOW, warn_hours=4)
    assert state == "in-flight"
    assert "23.0 hour(s)" in detail


def test_a_window_that_closed_while_the_status_still_says_running():
    state, detail = verdict(batch(expires_at=NOW - HOUR), NOW)
    assert state == "overdue"
    assert "1.0 hour(s) past" in detail


def test_the_deadline_says_which_timestamp_it_came_from():
    assert deadline({"expires_at": NOW}) == (NOW, "expires_at")
    when, source = deadline({"in_progress_at": NOW - HOUR})
    assert when == NOW - HOUR + 86400
    assert source == "in_progress_at plus 24h"
    when, source = deadline({"created_at": NOW - HOUR})
    assert when == NOW - HOUR + 86400
    assert "upper bound" in source
    assert deadline({"id": "b"})[0] is None


def test_expires_at_wins_over_the_fallbacks():
    # A long validating queue makes created_at plus 24h too generous, so the
    # API's own answer is preferred whenever the object carries it.
    when, source = deadline({"created_at": NOW - 6 * HOUR,
                             "in_progress_at": NOW - HOUR,
                             "expires_at": NOW + 2 * HOUR})
    assert when == NOW + 2 * HOUR
    assert source == "expires_at"


def test_settled_and_unreadable_batches_are_not_findings():
    for status in ("completed", "failed", "cancelled"):
        assert verdict(batch(status=status), NOW)[0] == "settled"
    assert verdict(batch(status="teleporting"), NOW)[0] == "unreadable"
    assert verdict(batch(), NOW)[0] == "unreadable"  # in flight, no timestamps
    assert counts_of({"request_counts": {"total": 5, "completed": 5}}) == (5, 5)
    assert counts_of({}) is None
