from openai_stored_state_probe import (RESPONSE_RETENTION_FLOOR_DAYS, age_days,
                                       coverage_note, grade_conversation,
                                       grade_response, item_totals,
                                       parse_records, repair_lines,
                                       response_row)

NOW = 1_800_000_000
DAY = 86400


def resp(rid, days_old, conversation=None):
    return response_row({"id": rid, "object": "response", "status": "completed",
                         "created_at": NOW - int(days_old * DAY),
                         "conversation": ({"id": conversation} if conversation
                                          else None),
                         "metadata": {"tenant": "acme"}})


def items(n, newest_days_old=1.0):
    return [{"id": "msg_%d" % i, "type": "message",
             "created_at": NOW - int((newest_days_old + n - i - 1) * DAY)}
            for i in range(n)]


def test_retention_is_read_as_a_floor_and_a_404_keeps_both_its_causes():
    state, detail = grade_response(resp("resp_a19", 94.2), 200, NOW, 30)
    assert state == "retained-past-policy"
    assert "still readable 94.2 day(s)" in detail
    assert "past your 30 day policy" in detail
    assert "at least %d days" % RESPONSE_RETENTION_FLOOR_DAYS in detail
    assert "a floor and not a deadline" in detail
    lines = repair_lines(state)
    assert any("DELETE /v1/responses/{response_id}" in line for line in lines)
    assert any("id ledger" in line for line in lines)
    # A 404 is one fact with two causes and the script names both.
    gone, gone_detail = grade_response(None, 404, NOW, 30)
    assert gone == "not-retained"
    assert "store false" in gone_detail and "aged out" in gone_detail
    assert repair_lines(gone) == []
    assert grade_response(resp("resp_z1", 1.0), 403, NOW, 30)[0] == "probe-unreadable"


def test_the_row_has_no_chain_in_it_and_no_store_flag():
    row = response_row({"id": "resp_b40", "created_at": 1_700_000_000,
                        "previous_response_id": "resp_a01", "status": "completed",
                        "conversation": {"id": "conv_x1"},
                        "metadata": {"tenant": "acme", "env": "prod"}})
    assert row == {"id": "resp_b40", "created_at": 1_700_000_000,
                   "status": "completed", "conversation": "conv_x1",
                   "metadata_keys": 2}
    # Following a parent is the other note. Reading store back is impossible.
    assert "previous_response_id" not in row
    assert "store" not in row and "stored" not in row
    assert response_row(None)["id"] == ""
    assert response_row({"created_at": "nonsense"})["created_at"] == 0
    assert response_row({"metadata": "nope"})["metadata_keys"] == 0


def test_deleting_the_conversation_does_not_delete_its_items():
    state, detail = grade_response(resp("resp_b40", 4.1, "conv_x1"), 200, NOW, 30)
    assert state == "items-outlive-response"
    assert "inside your policy" in detail
    assert "conv_x1" in detail and "retained until deleted" in detail
    lines = repair_lines(state)
    assert any("not enough here" in line for line in lines)
    assert any("items/{item_id}" in line for line in lines)
    assert any("does not delete its items" in line for line in lines)
    # And the same warning rides along when the response is also over policy.
    over = grade_response(resp("resp_c11", 91.0, "conv_x1"), 200, NOW, 30)
    assert over[0] == "retained-past-policy"
    assert "retained until deleted" in over[1]


def test_volume_and_idleness_are_two_findings_on_one_object():
    busy = item_totals(items(4182, newest_days_old=0.5))
    assert busy["count"] == 4182 and busy["newest"] > busy["oldest"]
    state, detail = grade_conversation({"id": "conv_x1"}, busy, 200, NOW, 30, 500)
    assert state == "thread-unbounded"
    assert "4182 item(s) and no TTL" in detail
    assert any("seeded with a summary" in line for line in repair_lines(state))
    # A small thread nobody has touched is the other finding entirely.
    idle = item_totals(items(12, newest_days_old=211.4))
    state, detail = grade_conversation({"id": "conv_y7"}, idle, 200, NOW, 30, 500)
    assert state == "thread-idle"
    assert "211.4 day(s) ago" in detail
    assert "retained until deleted" in detail
    # Busy and recent is neither.
    fine = item_totals(items(12, newest_days_old=1.0))
    assert grade_conversation({}, fine, 200, NOW, 30, 500)[0] == "thread-within-policy"
    assert grade_conversation(None, None, 404, NOW, 30, 500)[0] == "not-retained"


def test_ids_are_routed_by_prefix_and_what_cannot_be_routed_is_kept():
    records = parse_records("resp_a19\n\n# exported 2026-08-31\nconv_x1\n"
                            "resp_a19\nlegacy-7742  # old schema\n   \nconv_y7\n")
    assert records["responses"] == ["resp_a19"]
    assert records["conversations"] == ["conv_x1", "conv_y7"]
    assert records["unrecognised"] == ["legacy-7742"]
    assert parse_records("") == {"responses": [], "conversations": [],
                                 "unrecognised": []}
    assert any("hole in a coverage figure" in line
               for line in repair_lines("unrecognised-id"))


def test_the_coverage_sentence_is_printed_whatever_the_run_found():
    note = coverage_note({"responses": ["resp_a19"] * 388,
                          "conversations": ["conv_x1"] * 22,
                          "unrecognised": ["x", "y"]})
    assert "412 id(s) supplied" in note
    assert "388 response(s), 22 conversation(s), 2 unroutable" in note
    assert "has a list endpoint" in note
    assert "your records and not your account" in note
    # Even an empty run says it, because the limitation is not a result.
    assert "has a list endpoint" in coverage_note({})
    assert "0 id(s) supplied" in coverage_note(None)
    assert age_days(0, NOW) is None and age_days("x", NOW) is None
    assert item_totals(None) == {"count": 0, "oldest": 0, "newest": 0}
