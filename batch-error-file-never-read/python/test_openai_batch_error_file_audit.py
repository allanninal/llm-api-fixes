from openai_batch_error_file_audit import days_left, verdict

# 2026-08-30T00:00:00Z. Fixed, because the retention boundary is the point.
NOW = 1788048000
DAY = 86400


def batch(status="completed", error_file_id="file_err", age_days=10):
    return {"id": "batch_test", "status": status,
            "error_file_id": error_file_id,
            "created_at": NOW - age_days * DAY}


def meta(size=4096, age_days=10):
    return {"id": "file_err", "bytes": size, "purpose": "batch_output",
            "created_at": NOW - age_days * DAY}


def test_an_error_file_nobody_fetched_is_the_finding():
    state, detail = verdict(batch(), meta(size=4096), set(), NOW)
    assert state == "unread"
    assert "4096 byte(s)" in detail
    assert "missing from the downstream table" in detail


def test_the_ingest_record_is_what_clears_it():
    state, _ = verdict(batch(), meta(), {"file_err"}, NOW)
    assert state == "fetched"


def test_retention_turns_a_task_into_a_hole():
    # 29 days old: one day left, and urgent.
    state, detail = verdict(batch(age_days=29), meta(age_days=29), set(), NOW)
    assert state == "expiring"
    assert "1 day(s)" in detail

    # 31 days old: the content is unrecoverable by any read call.
    state, detail = verdict(batch(age_days=31), meta(age_days=31), set(), NOW)
    assert state == "aged-out"
    assert "not retrievable" in detail


def test_a_missing_file_object_reads_differently_inside_and_outside_the_window():
    assert verdict(batch(age_days=40), None, set(), NOW)[0] == "aged-out"
    assert verdict(batch(age_days=2), None, set(), NOW)[0] == "unresolvable"


def test_an_empty_error_file_is_not_a_pile_of_failures():
    state, detail = verdict(batch(), meta(size=0), set(), NOW)
    assert state == "empty"
    assert "never written to" in detail


def test_batches_with_nothing_to_read_are_left_alone():
    assert verdict(batch(error_file_id=None), None, set(), NOW)[0] == "no-error-file"
    assert verdict(batch(error_file_id=""), None, set(), NOW)[0] == "no-error-file"
    assert verdict(batch(status="in_progress"), meta(), set(), NOW)[0] == "running"


def test_days_left_floors_and_admits_ignorance():
    assert days_left(NOW - 10 * DAY, NOW) == 20
    assert days_left(NOW - int(29.9 * DAY), NOW) == 1
    assert days_left(NOW - 30 * DAY, NOW) == 0
    assert days_left(None, NOW) is None
    assert days_left("yesterday", NOW) is None
