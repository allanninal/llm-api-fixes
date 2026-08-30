from openai_orphaned_assistant_files import (age_days, class_state,
                                            classify_file, file_row, human,
                                            referenced_ids, repair_lines,
                                            summarise)

NOW = 1_800_000_000
DAY = 86400


def f(fid, size=1024, purpose="assistants", days_old=500):
    return file_row({"id": fid, "bytes": size, "purpose": purpose,
                     "filename": fid + ".pdf",
                     "created_at": NOW - int(days_old * DAY)})


def test_a_file_no_surviving_store_holds_is_the_finding():
    row = f("file-3ab", 43_200_512, days_old=511)
    state, detail = classify_file(row, set(), True, NOW)
    assert state == "orphan"
    assert "no surviving vector store holds this id" in detail
    assert "41.2 MiB" in detail and "511 day(s) ago" in detail
    lines = repair_lines(state, 1, 43_200_512)
    assert any("DELETE /v1/files/{file_id}" in line for line in lines)
    assert any("every vector store holding it" in line for line in lines)


def test_platform_generated_output_is_its_own_state():
    row = f("file-b19", 120_832, purpose="assistants_output", days_old=502)
    state, detail = classify_file(row, set(), True, NOW)
    assert state == "orphan-output"
    assert "code interpreter output" in detail
    assert "no longer exists" in detail
    # A file a live store still holds is not a finding at all.
    held, held_detail = classify_file(f("file-c04"), {"file-c04"}, True, NOW)
    assert held == "still-referenced"
    assert "still reads it" in held_detail
    assert repair_lines(held) == []


def test_one_unreadable_store_downgrades_every_verdict_in_the_run():
    row = f("file-3ab")
    # With a complete set this is a deletion candidate.
    assert classify_file(row, set(), True, NOW)[0] == "orphan"
    # With an incomplete one it is not, and the reason is stated.
    state, detail = classify_file(row, set(), False, NOW)
    assert state == "subtraction-incomplete"
    assert "could not be listed" in detail
    assert "cannot be called an orphan" in detail
    # Not even a file that really is referenced escapes the downgrade, because
    # the script cannot tell the two apart once the set is partial.
    assert classify_file(f("file-c04"), {"file-c04"}, False, NOW)[0] \
        == "subtraction-incomplete"
    assert class_state([row], False)[0] == "subtraction-unsafe"
    lines = repair_lines("subtraction-incomplete", unreadable=["vs_b2", "vs_a1"])
    assert "vs_a1, vs_b2" in lines[0]
    assert "perfectly well referenced" in lines[0]


def test_referenced_ids_reads_the_store_files_own_id():
    ids = referenced_ids([{"id": "file-c04", "object": "vector_store.file",
                           "vector_store_id": "vs_a1", "status": "completed"},
                          {"id": "file-d15", "status": "failed"},
                          {"id": ""}, None, "not-an-object", {}])
    assert ids == {"file-c04", "file-d15"}
    assert referenced_ids(None) == set()
    # A failed attach still holds the id, so the file is still referenced.
    assert classify_file(f("file-d15"), ids, True, NOW)[0] == "still-referenced"


def test_an_empty_purpose_class_is_an_answer_and_not_a_blank():
    state, detail = class_state([], True)
    assert state == "class-empty"
    assert "nothing was left behind" in detail
    assert repair_lines(state) == []
    full, full_detail = class_state([f("file-1"), f("file-2")], True)
    assert full == "class-populated"
    assert "2 file(s)" in full_detail
    assert "no longer exists" in full_detail


def test_the_folds_and_the_formatting_survive_junk():
    graded = [("orphan", f("file-1", 1024)),
              ("orphan", f("file-2", 2048)),
              ("still-referenced", f("file-3", 4096))]
    assert summarise(graded)["orphan"] == {"count": 2, "bytes": 3072}
    assert summarise([])== {}
    assert file_row(None)["id"] == ""
    assert file_row({"bytes": "nope", "created_at": "nope"})["size"] == 0
    assert age_days(0, NOW) is None and age_days("x", NOW) is None
    assert human(1024) == "1.0 KiB" and human(None) == "0 B"
