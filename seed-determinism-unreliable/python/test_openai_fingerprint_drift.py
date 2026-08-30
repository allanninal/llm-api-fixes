from openai_fingerprint_drift import (by_model, flatten, interleaved, iso,
                                     repair_lines, transitions, verdict, within)

DAY = 86400


def page(*rows):
    return {"object": "list", "data": list(rows), "has_more": False}


def completion(cid, created, model, fingerprint):
    return {"id": cid, "object": "chat.completion", "created": created,
            "model": model, "system_fingerprint": fingerprint}


def test_two_fingerprints_in_order_are_the_finding_with_a_date():
    rows = flatten([page(completion("c_1", 1000, "gpt-5.6-sol", "fp_aa11"),
                         completion("c_2", 2000, "gpt-5.6-sol", "fp_aa11"),
                         completion("c_3", 3000, "gpt-5.6-sol", "fp_bb22"))])
    entries = by_model(rows)["gpt-5.6-sol"]
    assert transitions(entries) == [(3000, "fp_aa11", "fp_bb22")]
    state, detail = verdict("gpt-5.6-sol", entries)
    assert state == "fingerprint-moved"
    assert "2 backend configurations" in detail and "switching once" in detail
    assert iso(3000) == "1970-01-01T00:50:00Z"
    assert any("not a test oracle" in line or "test oracle" in line
               for line in repair_lines(state))


def test_an_absent_fingerprint_is_a_finding_and_never_a_quiet_pass():
    rows = flatten([page(completion("c_1", 1000, "gpt-5.6-terra", None),
                         completion("c_2", 2000, "gpt-5.6-terra", ""))])
    entries = by_model(rows)["gpt-5.6-terra"]
    assert transitions(entries) == []
    state, detail = verdict("gpt-5.6-terra", entries)
    assert state == "fingerprint-absent"
    assert "even in principle" in detail
    lines = repair_lines(state)
    assert any("no signal to alarm on" in line for line in lines)
    # Never phrased as stability. Nothing was observed.
    assert not any("stable" in line for line in lines)


def test_an_interleaved_fleet_is_separated_from_one_dated_switchover():
    mixed = [{"fingerprint": "fp_aa11"}, {"fingerprint": "fp_bb22"},
             {"fingerprint": "fp_aa11"}]
    once = [{"fingerprint": "fp_aa11"}, {"fingerprint": "fp_aa11"},
            {"fingerprint": "fp_bb22"}]
    assert interleaved(mixed) and not interleaved(once)
    state, detail = verdict("gpt-5.6-sol", mixed)
    assert state == "fingerprint-moved"
    assert "more than one configuration is being served at once" in detail
    assert any("minutes apart" in line for line in repair_lines(state, True))
    assert not any("minutes apart" in line for line in repair_lines(state, False))


def test_one_fingerprint_is_a_reading_rather_than_a_comparison():
    single = [{"fingerprint": "fp_aa11"}]
    assert verdict("gpt-5.6-sol", single)[0] == "single-observation"
    steady = [{"fingerprint": "fp_aa11"}] * 40
    state, detail = verdict("gpt-5.6-sol", steady)
    assert state == "fingerprint-stable"
    assert "best effort" in detail
    assert any("not a promise" in line for line in repair_lines(state))


def test_an_empty_listing_points_at_store_and_at_the_responses_api():
    state, detail = verdict("(any model)", [])
    assert state == "nothing-stored"
    assert "no stored completions" in detail
    lines = repair_lines(state)
    assert any("store: true" in line for line in lines)
    assert any("Responses API" in line and "cannot be listed" in line
               for line in lines)


def test_rows_are_ordered_before_transitions_are_read_off_them():
    rows = flatten([page(completion("c_2", 2000, "m", "fp_aa11"),
                         completion("c_3", 3000, "m", "fp_bb22"),
                         completion("c_1", 1000, "m", "fp_aa11"))])
    # Unsorted, this reads as two transitions. Sorted, it is one.
    assert len(transitions(rows)) == 2
    assert transitions(by_model(rows)["m"]) == [(3000, "fp_aa11", "fp_bb22")]
    assert len(within(by_model(rows)["m"], 1500)) == 2
    assert within(rows, 0) == rows
    assert iso("nonsense") == "" and iso(None) == ""
