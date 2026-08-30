from openai_streaming_verification_probe import (INFERRED, MEASURED, by_model,
                                                 contrast, flatten, key_state,
                                                 repair_lines, verdict)


def bucket(*results):
    return {"start_time": 1_700_000_000, "results": list(results)}


def result(model, key_id, requests, input_tokens=0, output_tokens=0):
    return {"model": model, "api_key_id": key_id,
            "num_model_requests": requests, "input_tokens": input_tokens,
            "output_tokens": output_tokens}


def test_two_keys_disagreeing_on_one_model_is_the_finding():
    rows = flatten([bucket(result("gpt-5.6", "key_9fA2", 1204),
                           result("gpt-5.6", "key_3bQ7", 900, 400_000, 812_004))])
    per_key = by_model(rows)["gpt-5.6"]
    state, detail = verdict(200, per_key)
    assert state == "verification-suspected"
    assert "key_9fA2" in detail and "key_3bQ7" in detail
    assert "1,204" in detail and "812,004" in detail


def test_every_key_mute_is_the_other_note_and_says_so():
    # The boundary. A parameter a model refuses by name is refused for every
    # key, so agreement between keys means this note has no evidence at all.
    per_key = by_model(flatten([bucket(result("o4-mini", "key_a", 400),
                                       result("o4-mini", "key_b", 900),
                                       result("o4-mini", "key_c", 30))]))["o4-mini"]
    state, detail = verdict(200, per_key)
    assert state == "model-wide-mute"
    assert "every caller sends" in detail
    assert any("reasoning-model parameter note" in line
               for line in repair_lines(state))
    assert repair_lines(state) and state not in ("verification-suspected",)


def test_one_key_on_a_model_is_unresolvable_and_is_not_graded():
    per_key = by_model(flatten([bucket(result("gpt-5.1", "key_only", 800))]))["gpt-5.1"]
    state, detail = verdict(200, per_key)
    assert state == "single-key-model"
    assert "nothing to compare it against" in detail
    lines = repair_lines(state)
    assert any("canary" in line for line in lines)
    assert any(line.startswith("measured:") for line in lines)


def test_a_model_that_does_not_resolve_belongs_to_the_model_list_note():
    per_key = by_model(flatten([bucket(result("gpt-4-0613", "key_a", 500),
                                       result("gpt-4-0613", "key_b", 500, 1, 9))]))["gpt-4-0613"]
    state, detail = verdict(404, per_key)
    assert state == "model-not-visible"
    assert "model-list note" in detail
    assert any("GET /v1/models" in line for line in repair_lines(state))


def test_rejected_before_generation_is_not_the_same_as_produced_nothing():
    assert key_state({"requests": 100, "input": 0, "output": 0}) == "mute"
    assert key_state({"requests": 100, "input": 900, "output": 0}) == "no-output"
    assert key_state({"requests": 100, "input": 900, "output": 4}) == "producing"
    assert key_state({"requests": 0, "input": 0, "output": 0}) == "idle"
    assert key_state({"requests": 5, "input": 0, "output": 0}, 20) == "idle"

    per_key = by_model(flatten([bucket(result("m", "key_a", 100, 900, 0))]))["m"]
    state, _ = contrast(per_key)
    assert state == "input-without-output"
    assert any("truncation or a refusal" in line for line in repair_lines(state))


def test_the_finding_separates_what_was_measured_from_what_was_inferred():
    lines = repair_lines("verification-suspected")
    assert lines[0] == "measured: " + MEASURED
    assert lines[1] == "inferred: " + INFERRED
    assert "No endpoint reports verification state" in INFERRED
    assert any("15 minutes" in line for line in lines)
    assert any("unset stream" in line for line in lines)
    assert any("already verified" in line for line in lines)


def test_counts_are_coerced_and_missing_fields_do_not_become_silence():
    rows = flatten([bucket({"model": None, "api_key_id": None,
                            "num_model_requests": "not-a-number"})])
    assert rows == [("(unattributed)", "(unattributed)", 0, 0, 0)]
    assert flatten(None) == [] and by_model(None) == {}
    assert contrast({})[0] == "no-traffic"
    assert verdict(None, {})[1].endswith("to rule out access)")
    assert repair_lines("healthy") == []
