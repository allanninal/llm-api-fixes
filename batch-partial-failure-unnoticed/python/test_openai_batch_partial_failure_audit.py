from openai_batch_partial_failure_audit import counts_of, verdict


def batch(status="completed", total=100, completed=100, failed=0, **extra):
    """A batch object shaped like GET /v1/batches returns them."""
    body = {"id": "batch_test", "status": status,
            "request_counts": {"total": total, "completed": completed,
                               "failed": failed}}
    body.update(extra)
    return body


def test_completed_does_not_mean_every_row_succeeded():
    # The whole note: these two facts are compatible and the status hides it.
    state, detail = verdict(batch(total=50000, completed=49131, failed=869))
    assert state == "partial"
    assert "869 of 50000" in detail
    assert "869 line(s) shorter" in detail


def test_a_clean_batch_needs_both_halves_of_the_arithmetic():
    assert verdict(batch(total=100, completed=100, failed=0))[0] == "clean"
    assert verdict(batch(total=100, completed=99, failed=1))[0] == "partial"


def test_rows_in_neither_column_are_their_own_finding():
    # Not failures. Abandoned rows: attempted by nobody, absent from the error
    # file, and the shape a closed completion window leaves behind.
    state, detail = verdict(batch(total=100, completed=60, failed=0))
    assert state == "unaccounted"
    assert "40 of 100" in detail
    assert "abandoned" in detail


def test_an_in_flight_batch_is_not_reconciled_yet():
    for status in ("validating", "in_progress", "finalizing", "cancelling"):
        state, detail = verdict(batch(status=status, total=100, completed=3))
        assert state == "running"
        assert "not final" in detail


def test_the_other_terminal_states_belong_to_the_sibling_notes():
    for status in ("failed", "expired", "cancelled"):
        assert verdict(batch(status=status, total=100, completed=4,
                             failed=0))[0] == "other-terminal"


def test_missing_counts_are_never_reported_as_clean():
    assert verdict({"id": "b", "status": "completed"})[0] == "unreadable"
    assert verdict({"id": "b", "status": "completed",
                    "request_counts": []})[0] == "unreadable"
    assert verdict({"id": "b"})[0] == "unreadable"
    assert verdict(batch(total=0, completed=0))[0] == "empty"


def test_counts_are_read_leniently_but_not_invented():
    assert counts_of({"request_counts": {"total": 10}}) == (10, 0, 0)
    assert counts_of({"request_counts": {"total": "10", "completed": "9",
                                         "failed": "1"}}) == (10, 9, 1)
    assert counts_of({"request_counts": {"total": "many"}}) is None
    assert counts_of({}) is None
