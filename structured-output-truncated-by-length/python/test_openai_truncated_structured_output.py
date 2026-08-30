import json

from openai_truncated_structured_output import (batch_line_verdict, ceiling_use,
                                                classify, incomplete_reason,
                                                json_state, output_text,
                                                reasoning_share, repair_lines)


def stored(text, *, status="completed", reason=None, cap=None, used=None,
           reasoning=None):
    body = {"id": "resp_1", "status": status,
            "output": [{"type": "message",
                        "content": [{"type": "output_text", "text": text}]}]}
    if reason:
        body["incomplete_details"] = {"reason": reason}
    if cap is not None:
        body["max_output_tokens"] = cap
    if used is not None:
        body["usage"] = {"output_tokens": used}
        if reasoning is not None:
            body["usage"]["output_tokens_details"] = {"reasoning_tokens": reasoning}
    return body


def test_an_incomplete_response_holding_a_json_prefix_is_the_whole_note():
    # 200, a body, a bill, and a record that stops inside a string.
    half = '{"invoice_id": "INV-8817", "lines": [{"sku": "AB-1", "note": "part'
    response = stored(half, status="incomplete", reason="max_output_tokens",
                      cap=1024, used=1024)
    assert incomplete_reason(response) == "max_output_tokens"
    assert json_state(half) == "truncated"
    assert ceiling_use(response) == 1.0

    state, detail = classify(response)
    assert state == "truncated-by-length"
    assert "valid prefix that never closes" in detail
    repairs = repair_lines(state, response)
    assert "incomplete_details.reason" in repairs[0]
    assert "1024 output tokens" in repairs[1]


def test_a_ceiling_eaten_by_reasoning_gets_its_own_state():
    # Same reason, same 200, and raising the cap is not the only repair on
    # offer: the answer never started because the thinking used the budget.
    response = stored("", status="incomplete", reason="max_output_tokens",
                      cap=2000, used=2000, reasoning=1900)
    assert reasoning_share(response) == 0.95
    state, detail = classify(response)
    assert state == "ceiling-spent-on-reasoning"
    assert "visible answer barely started" in detail
    assert "reasoning effort" in " ".join(repair_lines(state, response))


def test_json_state_separates_a_cut_document_from_a_wrong_one():
    assert json_state('{"a": 1}') == "parses"
    assert json_state('{"a": [1, 2,') == "truncated"
    assert json_state('{"a": "unter') == "truncated"
    assert json_state('{"a": "esc\\\\') == "truncated"
    # A trailing comma closes every bracket it opened, so nothing was cut:
    # the model wrote invalid JSON and finished doing it.
    assert json_state('{"a": 1,}') == "malformed"
    assert json_state("Sorry, I cannot help with that.") == "malformed"
    assert json_state("   ") == "empty"
    assert json_state(None) == "empty"


def test_a_refusal_and_a_filter_stop_are_handed_to_the_other_note():
    refusal = {"status": "completed",
               "output": [{"type": "message",
                           "content": [{"type": "refusal",
                                        "refusal": "I can't help with that."}]}]}
    state, detail = classify(refusal)
    assert state == "refused"
    assert "Nothing was cut" in detail

    filtered = stored("", status="incomplete", reason="content_filter")
    assert classify(filtered)[0] == "stopped-by-filter"
    assert "refusal note" in classify(filtered)[1]


def test_a_completed_response_that_still_fails_to_parse_is_not_this_note():
    # Finished, and broken in a way a ceiling cannot explain. That is a schema
    # that was never enforced, and it has its own note.
    state, detail = classify(stored('{"total": 12,}'))
    assert state == "schema-not-followed"
    assert "advisory schema" in detail
    assert classify(stored('{"total": 12}'))[0] == "complete"
    assert classify(stored('{"total": 12,')) [0] == "cut-without-a-reason"


def test_chat_completions_rows_are_read_as_well_as_responses_rows():
    legacy = {"choices": [{"finish_reason": "length",
                           "message": {"content": '{"rows": [{"id": 1'}}]}
    assert output_text(legacy) == '{"rows": [{"id": 1'
    assert incomplete_reason(legacy) == "max_output_tokens"
    assert classify(legacy)[0] == "truncated-by-length"


def test_a_missing_ceiling_is_not_a_ceiling_of_zero():
    assert ceiling_use(stored("{}")) is None
    assert ceiling_use(stored("{}", cap=0, used=0)) is None
    assert ceiling_use(None) is None
    assert reasoning_share(stored("{}", cap=10, used=0)) is None
    assert classify(None)[0] == "empty-output"


def test_batch_results_are_keyed_by_custom_id_and_read_line_by_line():
    cut = json.dumps({"custom_id": "row-9", "result": {
        "type": "succeeded",
        "message": {"stop_reason": "max_tokens",
                    "usage": {"output_tokens": 4096},
                    "content": [{"type": "text", "text": '{"a": 1'}]}}})
    assert batch_line_verdict(cut)[:2] == ("row-9", "truncated-by-length")
    assert "4096" in batch_line_verdict(cut)[2]

    tool = json.dumps({"custom_id": "row-10", "result": {
        "type": "succeeded",
        "message": {"stop_reason": "max_tokens",
                    "content": [{"type": "tool_use", "name": "charge",
                                 "input": {}}]}}})
    assert batch_line_verdict(tool)[1] == "truncated-tool-use"
    assert "cannot be executed" in batch_line_verdict(tool)[2]

    done = json.dumps({"custom_id": "row-11", "result": {
        "type": "succeeded", "message": {"stop_reason": "end_turn",
                                         "content": []}}})
    assert batch_line_verdict(done)[1] == "complete"
    errored = json.dumps({"custom_id": "row-12", "result": {"type": "errored"}})
    assert batch_line_verdict(errored)[1] == "not-succeeded"
    assert batch_line_verdict("{not json")[1] == "unreadable"
    assert batch_line_verdict("")[1] == "unreadable"
