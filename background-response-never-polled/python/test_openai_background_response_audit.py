from openai_background_response_audit import (age_of, classify, error_code,
                                              read_ids, reason_for,
                                              repair_lines, summarise, verdict)

NOW = 1_800_000_000
SLA = 30 * 60


def record(status, created=None, http=200, **extra):
    body = {"id": "resp_x", "status": status}
    if created is not None:
        body["created_at"] = created
    body.update(extra)
    return {"http": http, "response": body, "created_hint": None}


def test_each_documented_status_gets_its_own_bucket():
    assert classify(record("completed", NOW - 60), NOW, SLA)[0] == "completed"
    assert classify(record("cancelled", NOW - 60), NOW, SLA)[0] == "cancelled"
    incomplete = record("incomplete", NOW - 60,
                        incomplete_details={"reason": "max_output_tokens"})
    bucket, detail = classify(incomplete, NOW, SLA)
    assert bucket == "incomplete" and "max_output_tokens" in detail
    failed = record("failed", NOW - 60,
                    error={"code": "server_error", "message": "boom"})
    bucket, detail = classify(failed, NOW, SLA)
    assert bucket == "failed" and "error.code server_error" in detail
    assert error_code(failed["response"]) == "server_error"
    # A status outside the enum is not silently treated as success.
    assert classify(record("weird", NOW), NOW, SLA)[0] == "unreadable"
    assert reason_for({}) == ""


def test_queued_is_normal_until_the_service_level_says_it_is_not():
    running = record("in_progress", NOW - 4 * 60)
    bucket, detail = classify(running, NOW, SLA)
    assert bucket == "running" and "inside the service level" in detail
    bucket, detail = classify(running, NOW, 3 * 60)
    assert bucket == "stranded" and detail.startswith("in_progress for 4 min")
    queued = record("queued", NOW - 19 * 3600)
    assert classify(queued, NOW, SLA)[0] == "stranded"
    # The hint from your own table stands in when the object has no created_at.
    no_stamp = {"http": 200, "response": {"status": "queued"},
                "created_hint": NOW - 7200}
    assert classify(no_stamp, NOW, SLA)[0] == "stranded"
    assert age_of({}, None, NOW) is None
    assert classify({"http": 200, "response": {"status": "queued"},
                     "created_hint": None}, NOW, SLA)[0] == "running"


def test_a_404_means_two_different_things_and_zdr_decides_which():
    lost = {"http": 404, "response": {}, "created_hint": NOW - 86400}
    assert classify(lost, NOW, SLA)[0] == "gone"
    bucket, detail = classify(lost, NOW, SLA, zdr=True)
    assert bucket == "aged-out" and "ten minutes" in detail
    # Inside the ZDR window a 404 is still a real miss.
    fresh = {"http": 404, "response": {}, "created_hint": NOW - 60}
    assert classify(fresh, NOW, SLA, zdr=True)[0] == "gone"
    assert classify({"http": 500, "response": {}}, NOW, SLA)[0] == "unreadable"


def test_the_id_file_takes_bare_ids_timestamps_comments_and_duplicates():
    text = "\n".join(["# open jobs", "", "resp_a", "resp_b,1799990000",
                      "resp_a", "  resp_c , not-a-number  "])
    assert read_ids(text) == [("resp_a", None), ("resp_b", 1799990000),
                              ("resp_c", None)]
    assert read_ids("") == []
    assert read_ids(None) == []


def test_an_empty_id_list_is_a_finding_and_not_a_clean_run():
    state, detail = verdict([], SLA)
    assert state == "background-no-ids"
    assert "no list endpoint" in detail
    lines = repair_lines(state, [])
    assert any("transactionally" in line for line in lines)
    drained = [{"id": "a", "bucket": "completed", "code": ""}]
    assert verdict(drained, SLA)[0] == "background-drained"
    assert summarise(drained) == {"completed": 1}


def test_transient_and_permanent_error_codes_get_different_repairs():
    rows = [
        {"id": "a", "bucket": "stranded", "code": ""},
        {"id": "b", "bucket": "failed", "code": "server_error"},
        {"id": "c", "bucket": "failed", "code": "invalid_prompt"},
        {"id": "d", "bucket": "gone", "code": ""},
        {"id": "e", "bucket": "incomplete", "code": ""},
    ]
    state, detail = verdict(rows, SLA)
    assert state == "background-stranded"
    assert "2 failed" in detail and "no longer retrievable" in detail
    lines = repair_lines(state, rows)
    retry = [line for line in lines if line.startswith("retry")]
    escalate = [line for line in lines if line.startswith("escalate")]
    assert retry and "server_error" in retry[0] and "invalid_prompt" not in retry[0]
    assert escalate and "invalid_prompt" in escalate[0]
    assert any("background true can be cancelled" in line for line in lines)
    assert any("incomplete_details.reason" in line for line in lines)
    assert summarise(rows)["stranded"] == 1
