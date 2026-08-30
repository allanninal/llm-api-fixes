from openai_refusal_channel import (classify, group_key, refusal_rate,
                                    refusals, repair_lines, stop_reason,
                                    visible_text)


def refused(text="I'm sorry, I can't help with that.", preamble=None,
            metadata=None):
    content = []
    if preamble:
        content.append({"type": "output_text", "text": preamble})
    content.append({"type": "refusal", "refusal": text})
    return {"id": "resp_r", "status": "completed", "model": "gpt-5.1",
            "metadata": metadata or {},
            "output": [{"type": "message", "content": content}]}


def answered(text='{"ok": true}', metadata=None):
    return {"id": "resp_a", "status": "completed", "model": "gpt-5.1",
            "metadata": metadata or {},
            "output": [{"type": "message",
                        "content": [{"type": "output_text", "text": text}]}]}


def test_a_refusal_is_a_completed_answer_with_nothing_to_parse():
    # The note in one assertion: 200, status completed, and the payload the
    # parser wanted is simply not the thing the model returned.
    response = refused()
    assert stop_reason(response) is None
    assert visible_text(response) == ""
    assert refusals(response) == [{"index": 0,
                                   "text": "I'm sorry, I can't help with that."}]

    state, detail = classify(response)
    assert state == "refused"
    assert "nothing went wrong" in detail
    assert "first-class branch before parsing" in repair_lines(state)[0]


def test_a_refusal_that_follows_a_preamble_is_not_an_answer_either():
    # The dangerous shape: concatenating the output items produces text, so a
    # naive reader stores the preamble as though it were the record.
    response = refused(preamble="Here is what I found so far. ")
    state, detail = classify(response)
    assert state == "refused-after-partial"
    assert "storing the preamble" in detail
    assert visible_text(response) == "Here is what I found so far."


def test_the_chat_completions_shape_is_read_as_well():
    legacy = {"choices": [{"finish_reason": "stop",
                           "message": {"content": None,
                                       "refusal": "I can't assist with that."}}]}
    assert refusals(legacy)[0]["text"] == "I can't assist with that."
    assert visible_text(legacy) == ""
    assert classify(legacy)[0] == "refused"


def test_a_filter_stop_is_counted_apart_from_a_model_refusal():
    filtered = {"status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "output": []}
    state, detail = classify(filtered)
    assert state == "stopped-by-filter"
    assert "not the model declining it" in detail
    assert "separately from model refusals" in repair_lines(state)[1]


def test_a_truncated_response_is_handed_to_the_other_note():
    # Same 200, same missing payload, and the repair is a ceiling rather than
    # a prompt. Getting these two confused costs an afternoon.
    cut = {"status": "incomplete",
           "incomplete_details": {"reason": "max_output_tokens"},
           "output": [{"type": "message",
                       "content": [{"type": "output_text", "text": '{"a": 1'}]}]}
    state, detail = classify(cut)
    assert state == "truncated"
    assert "Nothing was refused" in detail
    assert "interrupted, not unwilling" in repair_lines(state)[0]


def test_the_rate_is_grouped_by_template_and_withheld_below_the_floor():
    rows = ([(group_key(refused(metadata={"template": "kyc-extract"})), "refused")] * 9
            + [(group_key(answered(metadata={"template": "kyc-extract"})), "answered")] * 21
            + [(group_key(refused(metadata={"template": "rare-path"})), "refused")])
    rates = refusal_rate(rows)
    assert set(rates) == {"kyc-extract", "rare-path"}
    assert rates["kyc-extract"]["total"] == 30
    assert rates["kyc-extract"]["refused"] == 9
    assert abs(rates["kyc-extract"]["rate"] - 0.3) < 1e-9
    # One refusal in one call is 100%, and printing that teaches people to
    # ignore the report.
    assert rates["rare-path"]["total"] == 1
    assert rates["rare-path"]["rate"] is None


def test_grouping_falls_back_without_pretending_it_is_sharp():
    assert group_key(refused(metadata={"template": "kyc-extract"})) == "kyc-extract"
    assert group_key({"prompt": {"id": "pmpt_9"}}) == "prompt:pmpt_9"
    assert group_key({"model": "gpt-5.1"}) == "model:gpt-5.1"
    assert group_key({}) == "model:unknown"
    assert group_key(None) == "model:unknown"


def test_normal_and_empty_responses_are_left_alone():
    assert classify(answered())[0] == "answered"
    assert refusals(answered()) == []
    assert refusals(None) == []
    assert classify({"status": "completed", "output": []})[0] == "empty-answer"
    assert refusal_rate([]) == {}
    assert refusal_rate(None) == {}
