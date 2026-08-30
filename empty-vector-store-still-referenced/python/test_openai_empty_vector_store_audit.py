from openai_empty_vector_store_audit import (cause, classify, configured_ids,
                                             counts, emptiness, repair_lines,
                                             usage_bytes)


def store(total=0, completed=0, failed=0, in_progress=0, bytes_=0,
          status="completed", sid="vs_a1", name="handbook"):
    return {"id": sid, "name": name, "status": status, "usage_bytes": bytes_,
            "file_counts": {"total": total, "completed": completed,
                            "failed": failed, "in_progress": in_progress,
                            "cancelled": 0}}


def test_a_configured_store_with_nothing_in_it_is_the_finding():
    empty = store(total=0)
    assert emptiness(empty) == "no-files"
    state, detail = classify(empty, referenced=True)
    assert state == "referenced-empty"
    assert "0 file(s) attached" in detail
    assert cause(empty) == "never-ingested"
    assert any("refuse to boot" in line
               for line in repair_lines(state, cause(empty)))


def test_attached_but_never_indexed_is_the_other_note():
    # The boundary. Forty files went in and none came out, which is an attach
    # failure wearing an empty store's symptoms, and it is repaired per
    # last_error.code rather than by re-running the ingest.
    broken = store(total=40, completed=0, failed=40)
    assert emptiness(broken) == "nothing-completed"
    state, detail = classify(broken, referenced=True)
    assert state == "referenced-nothing-indexed"
    assert "40 attached, 0 completed" in detail
    assert cause(broken) == "attach-failed"
    assert any("last_error.code" in line
               for line in repair_lines(state, cause(broken)))


def test_an_expired_store_is_empty_for_a_reason_that_will_recur():
    gone = store(total=0, status="expired")
    assert cause(gone) == "expired"
    lines = repair_lines("referenced-empty", cause(gone))
    assert any("same schedule" in line for line in lines)
    # And the counts alone cannot tell you: they are identical either way.
    assert counts(gone) == counts(store(total=0))


def test_an_empty_store_nobody_references_is_not_a_finding():
    state, detail = classify(store(total=0), referenced=False)
    assert state == "abandoned-empty"
    assert "litter" in detail
    assert classify(store(total=9, completed=9, bytes_=1024),
                    referenced=False)[0] == "unreferenced"


def test_an_id_that_does_not_resolve_blames_the_project_first():
    state, detail = classify(None, referenced=True)
    assert state == "referenced-missing"
    assert "project scoped" in detail
    lines = repair_lines(state)
    assert "project" in lines[0]
    assert classify(None, referenced=False)[0] == "not-found"


def test_completed_files_with_no_bytes_is_named_rather_than_guessed():
    odd = store(total=9, completed=9, bytes_=0)
    assert emptiness(odd) == "zero-bytes"
    state, detail = classify(odd, referenced=True)
    assert state == "referenced-zero-bytes"
    assert "disagree" in detail
    assert any("before deciding" in line for line in repair_lines(state))


def test_configured_ids_survives_the_trailing_comma():
    assert configured_ids("vs_a1,vs_b2,") == ["vs_a1", "vs_b2"]
    assert configured_ids("vs_a1 vs_b2\nvs_a1") == ["vs_a1", "vs_b2"]
    assert configured_ids(None, ["vs_c3"], "vs_c3") == ["vs_c3"]
    assert configured_ids("") == [] and configured_ids() == []


def test_a_grounded_store_reports_its_size():
    good = store(total=812, completed=812, bytes_=43_200_512)
    state, detail = classify(good, referenced=True)
    assert state == "grounded"
    assert "41.2 MiB" in detail
    assert repair_lines(state) == []
    assert usage_bytes({"usage_bytes": "nope"}) == 0
    assert emptiness(None) == "no-files"
