from anthropic_version_header_probe import (ABSENT, CURRENT, INITIAL,
                                            classify_status,
                                            declared_findings,
                                            gateway_verdict, host_verdict,
                                            probe_headers, probe_labels,
                                            repair_lines)


def matrix(absent=400, current=200, ancient=200, **extra):
    out = {ABSENT: absent, CURRENT: current, INITIAL: ancient}
    out.update(extra)
    return out


def test_the_pair_of_probes_is_what_proves_the_header_is_required():
    # One status code says nothing. Absent-400 next to current-200 is the whole
    # claim, and flipping the absent probe to 200 inverts the verdict.
    state, detail = host_verdict(matrix())
    assert state == "version-enforced"
    assert "400 without the header" in detail

    state, detail = host_verdict(matrix(absent=200))
    assert state == "version-not-enforced"
    assert "gateway on this path is adding it" in detail
    assert classify_status(ABSENT, 200)[1].endswith("supplying one for you")
    assert any("does not have" in line for line in repair_lines(state))


def test_a_gateway_that_injects_the_header_is_only_visible_from_two_hosts():
    state, detail = gateway_verdict(matrix(absent=400), matrix(absent=200))
    assert state == "gateway-injects"
    assert "Every client behind it is untested" in detail
    lines = repair_lines(state)
    assert any("in the client itself" in line for line in lines)
    assert any("official SDK" in line for line in lines)


def test_a_gateway_that_strips_the_header_is_the_mirror_case():
    state, detail = gateway_verdict(matrix(), matrix(current=400))
    assert state == "gateway-strips"
    assert "stripped or rewritten in transit" in detail

    # Same statuses on both paths is not a finding, and a missing gateway is a
    # statement about coverage rather than about health.
    assert gateway_verdict(matrix(), matrix())[0] == "gateway-agrees"
    state, detail = gateway_verdict(matrix(), {})
    assert state == "no-gateway"
    assert "invisible to a single host" in detail


def test_a_matrix_you_cannot_authenticate_is_not_evidence_about_a_header():
    # The gate. A 401 on the current-version probe means the key is the story,
    # and grading the absent probe on top of it would invent a header problem.
    state, detail = host_verdict(matrix(absent=401, current=401))
    assert state == "current-rejected"
    assert "credential problem" in detail
    assert classify_status(ABSENT, 401)[0] == "credentials"
    assert host_verdict(matrix(current=None))[0] == "unreachable"
    assert host_verdict({})[0] == "unreachable"


def test_the_absent_probe_really_sends_no_version_header():
    # A bug here turns the whole script into three identical probes that all
    # pass, so it is asserted rather than assumed.
    assert probe_headers(ABSENT) == {}
    assert probe_headers(CURRENT) == {"anthropic-version": "2023-06-01"}
    assert probe_headers("  2023-01-01 ") == {"anthropic-version": "2023-01-01"}
    assert probe_labels([]) == [ABSENT, CURRENT, INITIAL]
    assert probe_labels(["2023-06-01", " ", "2024-06-01", "2024-06-01"]) == [
        ABSENT, CURRENT, INITIAL, "2024-06-01"]


def test_declared_versions_are_graded_against_the_history_not_the_status():
    rows = declared_findings(matrix(**{"2024-06-01": 400}),
                             [CURRENT, INITIAL, "2024-06-01"])
    states = {version: state for version, state, _ in rows}
    assert CURRENT not in states
    assert states[INITIAL] == "ancient-pinned"
    assert states["2024-06-01"] == "unknown-version-pinned"

    ancient = [d for v, _, d in rows if v == INITIAL][0]
    assert "data: [DONE]" in ancient
    assert "this host returns 200 for it" in ancient
    assert any("2023-06-01" in line for line in repair_lines("ancient-pinned"))


def test_single_statuses_are_described_and_never_promoted_to_verdicts():
    assert classify_status(INITIAL, 200)[0] == "accepted-deprecated"
    assert classify_status(INITIAL, 410)[0] == "refused"
    assert classify_status("2024-06-01", 200)[0] == "accepted-unknown"
    assert classify_status(CURRENT, 529)[0] == "unexpected"
    assert classify_status(CURRENT, None)[0] == "unreachable"
    assert repair_lines("version-enforced") == []
