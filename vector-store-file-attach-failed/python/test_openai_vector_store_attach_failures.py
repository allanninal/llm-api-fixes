from openai_vector_store_attach_failures import (UNREPORTED, bucket_errors,
                                                  counts, failure_rate,
                                                  reconcile, repair_lines,
                                                  stalled, verdict)


def store(total=0, completed=0, failed=0, in_progress=0, cancelled=0,
          status="completed"):
    return {"id": "vs_a1", "name": "handbook", "status": status,
            "file_counts": {"total": total, "completed": completed,
                            "failed": failed, "in_progress": in_progress,
                            "cancelled": cancelled}}


def child(fid, status, code=None, created_at=1_700_000_000):
    row = {"id": fid, "object": "vector_store.file", "status": status,
           "created_at": created_at, "vector_store_id": "vs_a1"}
    row["last_error"] = {"code": code, "message": "..."} if code else None
    return row


def test_a_completed_store_with_failed_children_is_the_finding():
    # The whole note. status is "completed" because nothing is pending, which
    # is exactly what an ingest job polls for before declaring the corpus ready.
    c = counts(store(total=849, completed=812, failed=37))
    buckets = bucket_errors(
        [child("file-9k%d" % i, "failed", "unsupported_file") for i in range(19)]
        + [child("file-7b%d" % i, "failed", "invalid_file") for i in range(14)]
        + [child("file-2d%d" % i, "failed", "server_error") for i in range(4)])
    state, detail = verdict(c, buckets, [])
    assert state == "attach-failed"
    assert "37 of 849" in detail
    assert sorted(buckets) == ["invalid_file", "server_error", "unsupported_file"]
    repairs = repair_lines(state, buckets)
    assert any("OCR" in line for line in repairs)
    assert any("file_counts.failed == 0" in line for line in repairs)


def test_an_empty_store_is_handed_to_the_other_note_by_name():
    # The boundary between this note and its closest neighbour, asserted rather
    # than described. total == 0 means nothing was ever attached; that is not a
    # zero per cent failure rate and it is not repaired the same way.
    c = counts(store(total=0))
    state, detail = verdict(c, {}, [])
    assert state == "no-files"
    assert "empty vector store note" in detail
    assert failure_rate(c) == 0.0
    assert any("vector_store_ids" in line for line in repair_lines(state))


def test_a_failed_child_with_no_last_error_keeps_its_own_bucket():
    buckets = bucket_errors([child("file-1", "failed", "invalid_file"),
                             child("file-2", "failed", None),
                             child("file-3", "completed", None)])
    assert buckets[UNREPORTED] == ["file-2"]
    assert buckets["invalid_file"] == ["file-1"]
    assert "completed" not in buckets
    assert any("has not been looked at" in line
               for line in repair_lines("attach-failed", buckets))


def test_the_summary_and_the_listing_can_disagree():
    # file_counts still counts 37 failures and the filtered listing returns
    # none: somebody detached the failed files and stopped there.
    state, detail = verdict(counts(store(total=812, completed=812, failed=37)),
                            {}, [])
    assert state == "counts-disagree"
    assert "half-finished repair" in detail
    assert any("ingest manifest" in line for line in repair_lines(state))
    assert reconcile({"failed": 37}, {}) == (37, 0)
    assert reconcile({"failed": 2}, {"server_error": ["a", "b"]}) == (2, 2)


def test_children_pinned_in_progress_are_measured_against_the_clock():
    now = 1_700_050_000
    rows = stalled([child("file-slow", "in_progress", created_at=now - 40_000),
                    child("file-newer", "in_progress", created_at=now - 20_000),
                    child("file-fresh", "in_progress", created_at=now - 60),
                    child("file-bad", "in_progress", created_at=None),
                    child("file-done", "completed", created_at=now - 90_000)],
                   now)
    assert [r[0] for r in rows] == ["file-slow", "file-newer"]
    state, detail = verdict(counts(store(total=5, completed=3, in_progress=2)),
                            {}, rows)
    assert state == "ingestion-stalled"
    assert "parent stays in_progress" in detail
    assert any("file-slow (11h)" in line
               for line in repair_lines(state, {}, rows))


def test_a_healthy_store_and_a_still_settling_one_are_not_findings():
    assert verdict(counts(store(total=40, completed=40)), {}, [])[0] == "complete"
    state, _ = verdict(counts(store(total=40, completed=38, in_progress=2)),
                       {}, [])
    assert state == "still-ingesting"
    assert repair_lines("complete") == []
    assert bucket_errors(None) == {} and stalled(None, 0) == []
    assert counts(None)["total"] == 0
    assert counts({"file_counts": {"total": "not-a-number"}})["total"] == 0


def test_an_unknown_error_code_is_reported_rather_than_bucketed_away():
    buckets = bucket_errors([child("file-x", "failed", "quota_exceeded")])
    lines = repair_lines("attach-failed", buckets)
    assert any("quota_exceeded" in line for line in lines)
    assert any("three documented values" in line for line in lines)
