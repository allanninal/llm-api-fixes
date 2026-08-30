from anthropic_beta_header_audit import (classify_probe, conflicting,
                                          graduation_verdict, key_sets,
                                          levenshtein, load_call_sites,
                                          near_matches, repair_lines,
                                          shape_delta, split_betas)


def files_listing(beta):
    """The documented Files API shapes, with and without files-api-2025-04-14."""
    if beta:
        return {"data": [{"id": "file_01", "type": "file", "size_bytes": 12}],
                "has_more": False, "first_id": "file_01", "last_id": "file_01"}
    return {"data": [{"id": "file_01", "type": "file", "size_bytes": 12,
                      "expires_at": None}],
            "next_page": None}


def test_a_misspelled_beta_is_rejected_and_the_suggestion_is_the_repair():
    state, detail = classify_probe("contxt-1m-2025-08-07", 400)
    assert state == "rejected-typo"
    # One message, two causes, and the script refuses to pick between them.
    assert "not entitled to" in detail
    matches = near_matches("contxt-1m-2025-08-07")
    assert matches[0] == "context-1m-2025-08-07"
    lines = repair_lines(state, "contxt-1m-2025-08-07", matches)
    assert any("context-1m-2025-08-07" in line for line in lines)
    assert any("entitlement" in line for line in lines)


def test_a_graduated_beta_returns_200_and_pins_the_older_shape():
    # The documented Files API migration table, asserted as a diff.
    deltas = {"/files": shape_delta(files_listing(True), files_listing(False))}
    state, detail = graduation_verdict("files-api-2025-04-14", deltas)
    assert state == "pinned-to-beta-shape"
    assert "/files" in detail
    assert deltas["/files"]["top"][0] == ("first_id", "has_more", "last_id")
    assert deltas["/files"]["top"][1] == ("next_page",)
    assert deltas["/files"]["item"][1] == ("expires_at",)
    lines = repair_lines(state, "files-api-2025-04-14", (), deltas)
    assert any("expires_at" in line for line in lines)
    assert any("graduated" in line for line in lines)


def test_identical_bodies_prove_nothing_and_are_not_a_finding():
    same = {"data": [{"id": "m1"}], "has_more": False}
    deltas = {"/models": shape_delta(same, same)}
    state, detail = graduation_verdict("context-management-2025-06-27", deltas)
    assert state == "no-visible-difference"
    assert "not evidence that the header does nothing" in detail
    assert repair_lines(state) == []


def test_the_published_enum_is_a_dictionary_and_not_the_verdict():
    state, detail = classify_probe("brand-new-beta-2026-09-01", 200)
    assert state == "accepted-undocumented"
    assert "the list is behind" in detail
    assert classify_probe("files-api-2025-04-14", 200)[0] == "accepted"
    assert near_matches("nothing-like-a-beta-name") == ()
    assert classify_probe("nothing-like-a-beta-name", 400)[0] == "rejected-unknown"
    assert levenshtein("abc", "abc") == 0 and levenshtein("", "abc") == 3


def test_the_header_is_one_string_carrying_a_list_so_it_can_be_malformed():
    names, faults = split_betas("files-api-2025-04-14, skills-2025-10-02,")
    assert names == ("files-api-2025-04-14", "skills-2025-10-02")
    assert any("trailing comma" in f for f in faults)

    names, faults = split_betas("Skills-2025-10-02,skills-2025-10-02")
    assert names == ("skills-2025-10-02",)
    assert any("lower case" in f for f in faults)
    assert any("more than once" in f for f in faults)

    names, faults = split_betas("files api 2025-04-14")
    assert any("whitespace inside" in f for f in list(names) + list(faults))
    assert any("comma separated" in line
               for line in repair_lines("malformed-header"))


def test_the_documented_conflicting_pair_needs_no_request_at_all():
    assert conflicting(["agent-memory-2026-07-22",
                        "managed-agents-2026-04-01"]) == [
        ("agent-memory-2026-07-22", "managed-agents-2026-04-01")]
    assert conflicting(["managed-agents-2026-04-01"]) == []
    assert any("replaces the second" in line
               for line in repair_lines("conflicting-pair"))


def test_input_and_bodies_are_read_in_whatever_shape_they_arrive():
    assert load_call_sites('{"a.py": "x,y"}') == {"a.py": "x,y"}
    assert load_call_sites('["x", "y"]') == {"(declared)": "x,y"}
    assert load_call_sites("x,y") == {"(declared)": "x,y"}
    assert load_call_sites("") == {}
    assert key_sets(None) == ((), ())
    assert key_sets({"data": []}) == (("data",), ())
    assert classify_probe("files-api-2025-04-14", None)[0] == "unreachable"
    assert classify_probe("files-api-2025-04-14", 401)[0] == "credentials"
