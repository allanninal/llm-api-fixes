from openai_fine_tune_failures import (classify_job, error_advice,
                                      error_events, hours_since, job_row,
                                      repair_lines)

NOW = 1_800_000_000
HOUR = 3600


def job(status, code="", param="", message="", hours_old=1.0, jid="ftjob_a1"):
    return job_row({"id": jid, "object": "fine_tuning.job", "status": status,
                    "model": "gpt-5.6-terra",
                    "created_at": NOW - int(hours_old * HOUR),
                    "fine_tuned_model": None, "trained_tokens": None,
                    "error": ({"code": code, "message": message,
                               "param": param} if code or message else None)})


def test_a_failed_job_names_the_rejected_input_and_the_documented_fix():
    row = job("failed", "invalid_training_file", "training_file",
              "The job failed due to an invalid training file.")
    state, detail = classify_job(row, NOW, 2.0)
    assert state == "job-failed"
    assert "failed on training_file with invalid_training_file" in detail
    lines = repair_lines(state, row["code"])
    assert lines[0] == error_advice("invalid_training_file")
    assert "no trailing blank line" in lines[0]
    assert any("receipt, not a result" in line for line in lines)


def test_an_unknown_code_is_printed_and_never_interpreted():
    row = job("failed", "some_new_code_2027", "training_file", "...")
    state, _ = classify_job(row, NOW, 2.0)
    assert state == "job-failed"
    assert error_advice("some_new_code_2027") == ""
    lines = repair_lines(state, row["code"])
    assert "some_new_code_2027" in lines[0]
    assert "do not act on a guess" in lines[0]
    # exceeded_quota is documented, and it is not a data problem.
    assert "billing problem" in error_advice("exceeded_quota")
    assert "Editing the file will not help" in error_advice("exceeded_quota")


def test_hours_in_validating_files_is_its_own_finding():
    stalled = job("validating_files", hours_old=9.4, jid="ftjob_b2")
    state, detail = classify_job(stalled, NOW, 2.0)
    assert state == "stalled-in-validation"
    assert "9.4 hours in validating_files" in detail
    assert any("dead upload" in line for line in repair_lines(state))
    # The same job an hour in is simply validating.
    fresh = job("validating_files", hours_old=1.0, jid="ftjob_b3")
    assert classify_job(fresh, NOW, 2.0)[0] == "validating"
    assert abs(hours_since(NOW - 5 * HOUR, NOW) - 5.0) < 1e-9
    assert hours_since(0, NOW) is None


def test_a_failure_with_no_error_object_is_sent_to_the_events_feed():
    row = job("failed")
    assert row["code"] == "" and row["param"] == ""
    state, detail = classify_job(row, NOW, 2.0)
    assert state == "failed-without-error"
    assert "the only account of why" in detail
    assert any("/events" in line for line in repair_lines(state))


def test_a_succeeded_job_is_handed_to_the_other_note():
    state, detail = classify_job(job("succeeded", hours_old=200.0), NOW, 2.0)
    assert state == "succeeded"
    assert "a different note" in detail
    assert repair_lines(state) == []
    assert classify_job(job("cancelled"), NOW, 2.0)[0] == "cancelled"
    assert classify_job(job("running"), NOW, 2.0)[0] == "running"
    assert classify_job(job("beaming_up"), NOW, 2.0)[0] == "unknown-status"


def test_the_events_feed_is_filtered_to_errors_and_kept_in_order():
    feed = [{"level": "info", "message": "Created fine-tuning job"},
            {"level": "error", "message": "line 4108 has no assistant message"},
            {"level": "warn", "message": "..."},
            {"level": "ERROR", "message": "line 4108 has no assistant message"},
            {"level": "error", "message": "validation failed"},
            "not a dict"]
    assert error_events(feed) == ["line 4108 has no assistant message",
                                  "validation failed"]
    assert error_events(None) == []
    assert job_row(None)["id"] == ""
    assert job_row({"created_at": "nonsense"})["created_at"] == 0
