from openai_batch_validation_audit import (batch_input_ids, error_rows,
                                            failed_batches, lines_by_code,
                                            mispurposed_inputs, nothing_billed,
                                            repair_lines, verdict, within_window)

NOW = 1_800_000_000

FAILED = {
    "id": "batch_aa",
    "status": "failed",
    "created_at": NOW - 3600,
    "failed_at": NOW - 3560,
    "input_file_id": "file_in1",
    "request_counts": {"total": 0, "completed": 0, "failed": 0},
    "errors": {"object": "list", "data": [
        {"code": "invalid_json", "message": "not valid JSON", "param": None,
         "line": 41207},
        {"code": "invalid_json", "message": "not valid JSON", "param": None,
         "line": 41208},
        {"code": "duplicate_custom_id", "message": "custom_id repeated",
         "param": "custom_id", "line": 903},
    ]},
}

RAN = {
    "id": "batch_bb",
    "status": "completed",
    "created_at": NOW - 7200,
    "input_file_id": "file_in2",
    "request_counts": {"total": 900, "completed": 880, "failed": 20},
    "output_file_id": "file_out2",
}


def test_a_failed_batch_never_ran_and_names_its_lines():
    assert [b["id"] for b in failed_batches([FAILED, RAN])] == ["batch_aa"]
    # Validation happens before dispatch, so the reassuring half is provable.
    assert nothing_billed(FAILED)
    assert not nothing_billed(RAN)
    groups = lines_by_code(error_rows(FAILED))
    assert groups["invalid_json"][0] == [41207, 41208]
    assert groups["invalid_json"][1] == 2
    assert groups["duplicate_custom_id"][0] == [903]
    assert groups["duplicate_custom_id"][3] == "custom_id"


def test_every_field_in_the_errors_object_is_allowed_to_be_missing():
    assert error_rows({"status": "failed"}) == []
    assert error_rows({"errors": None}) == []
    assert error_rows({"errors": {"data": None}}) == []
    assert error_rows({"errors": {"data": ["not an object"]}}) == []
    rows = error_rows({"errors": {"data": [{"code": None, "line": None}]}})
    assert rows == [{"code": "unknown", "message": "", "param": None,
                     "line": None}]
    # A code with no line still gets a row, worded so nobody hunts for line 0.
    assert lines_by_code(rows)["unknown"][0] == []
    # An absent request_counts is what a batch that never started looks like.
    assert nothing_billed({"status": "failed"})


def test_a_mispurposed_input_needs_all_three_conditions():
    files = [
        {"id": "file_x", "filename": "nightly.jsonl", "purpose": "user_data",
         "bytes": 1400000},
        {"id": "file_ok", "filename": "nightly.jsonl", "purpose": "batch",
         "bytes": 1400000},
        {"id": "file_in2", "filename": "used.jsonl", "purpose": "user_data",
         "bytes": 10},
        {"id": "file_img", "filename": "photo.png", "purpose": "vision",
         "bytes": 900},
        {"id": "file_res", "filename": "out.jsonl", "purpose": "batch_output",
         "bytes": 50},
    ]
    used = batch_input_ids([FAILED, RAN])
    assert used == {"file_in1", "file_in2"}
    found = mispurposed_inputs(files, used)
    assert [r["id"] for r in found] == ["file_x"]
    assert found[0]["purpose"] == "user_data"
    # Outputs are not inputs, and a file that was used is never a finding.
    assert mispurposed_inputs(files, {"file_x"} | used) == []


def test_rows_that_failed_inside_a_batch_that_ran_belong_to_another_note():
    # request_counts.failed > 0 on a completed batch is the published
    # partial-failure note. This script must not claim it.
    assert failed_batches([RAN]) == []
    state, detail = verdict([], [], 30)
    assert state == "validation-clean"
    assert "no batch in the last 30 days" in detail


def test_the_window_is_arithmetic_on_created_at_and_zero_means_everything():
    assert within_window(FAILED, NOW, 30)
    assert not within_window(FAILED, NOW + 40 * 86400, 30)
    assert within_window(FAILED, NOW + 40 * 86400, 0)
    assert not within_window({"created_at": "nonsense"}, NOW, 30)


def test_the_repair_names_the_documented_fix_for_the_code_it_saw():
    state, detail = verdict([FAILED], [{"id": "file_x"}], 30)
    assert state == "validation-failed"
    assert "will not accept" in detail
    lines = repair_lines(state, ["duplicate_custom_id", "invalid_json", "made_up"])
    assert any("custom_id is the only join key" in line for line in lines)
    assert any("every line must parse on its own" in line for line in lines)
    assert not any("made_up" in line for line in lines)
    assert any("receipt, not a result" in line for line in lines)
    assert repair_lines("validation-clean", [])[0].startswith("nothing to change")
    orphan_only = verdict([], [{"id": "file_x"}], 0)
    assert orphan_only[0] == "orphan-input-files"
    assert any("purpose matches the endpoint"
               in line for line in repair_lines(orphan_only[0], []))
