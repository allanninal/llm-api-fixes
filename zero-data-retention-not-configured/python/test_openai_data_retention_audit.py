from openai_data_retention_audit import (archived, classify, effective,
                                          family, repair_lines, residency_note)

EU = {"id": "proj_eu", "name": "EU tenant", "status": "active",
      "residency": "EU_STORAGE_PROCESSING"}


def test_the_same_project_resolves_two_ways_under_two_org_defaults():
    # The note. Nothing on proj_eu changes between these two readings.
    state, detail = classify(EU, "enhanced_zero_data_retention",
                             "organization_default", "zdr")
    assert state == "inherited-not-pinned"
    assert "only because the organization default says so" in detail
    assert any("moves the day somebody changes" in line
               for line in repair_lines(state, EU, "zdr"))

    state, detail = classify(EU, "modified_abuse_monitoring",
                             "organization_default", "zdr")
    assert state == "weaker-than-claimed"
    assert "inherited from the organization" in detail
    assert "zero data retention was claimed" in detail

    # And pinned on the project is a pass, not a finding.
    assert classify(EU, "modified_abuse_monitoring",
                    "zero_data_retention", "zdr")[0] == "compliant"


def test_none_is_never_treated_as_an_inherit():
    state, detail = classify({"id": "proj_ingest"}, "enhanced_zero_data_retention",
                             "none", "zdr")
    assert state == "no-retention-control"
    assert "whatever the organization default says" in detail
    # The org default is a ZDR variant and the project still fails, which is the
    # whole reason none has its own state.
    assert effective("enhanced_zero_data_retention", "none") == ("none", False)
    assert effective("enhanced_zero_data_retention", "organization_default") == \
        ("enhanced_zero_data_retention", True)
    assert effective("zero_data_retention", None) == (None, False)


def test_the_family_map_groups_without_ranking():
    assert family("zero_data_retention") == "zdr"
    assert family("enhanced_zero_data_retention") == "zdr"
    assert family("modified_abuse_monitoring") == "modified-abuse-monitoring"
    assert family("enhanced_modified_abuse_monitoring") == "modified-abuse-monitoring"
    assert family("none") == "none"
    assert family(None) == "unreadable"
    assert family("standard") == "unrecognised"
    # Claiming the weaker family makes a MAM project pass, and that is a
    # decision the caller makes rather than one the script makes for them.
    project = {"id": "proj_a"}
    assert classify(project, None, "modified_abuse_monitoring",
                    "modified-abuse-monitoring")[0] == "compliant"
    assert classify(project, None, "modified_abuse_monitoring",
                    "zdr")[0] == "weaker-than-claimed"


def test_an_unrecognised_value_is_never_graded_as_safe():
    state, detail = classify({"id": "proj_x"}, "zero_data_retention",
                             "legacy_mode", "zdr")
    assert state == "retention-unreadable"
    assert "will not grade as safe" in detail
    assert any("Read it by hand" in line for line in repair_lines(state,
                                                                  {"id": "proj_x"}))
    # A project the endpoint would not answer for is unreadable too.
    assert classify({"id": "proj_y"}, "zero_data_retention", None,
                    "zdr")[0] == "retention-unreadable"


def test_archived_projects_are_graded_and_labelled():
    old = {"id": "proj_old", "archived_at": 1_700_000_000}
    assert archived(old) is True
    assert archived({"id": "p", "status": "archived"}) is True
    assert archived({"id": "p", "status": "active"}) is False
    state, detail = classify(old, "modified_abuse_monitoring", "none", "zdr")
    assert state == "no-retention-control"
    assert "its retained data is still retained" in detail


def test_residency_is_a_separate_axis_and_absent_is_not_global():
    ok, detail = residency_note(EU, "EU_STORAGE_PROCESSING")
    assert ok is True and detail is None
    ok, detail = residency_note({"id": "p", "residency": "US_STORAGE_PROCESSING"},
                                "EU_STORAGE_PROCESSING")
    assert ok is False
    assert "residency is US_STORAGE_PROCESSING" in detail
    ok, detail = residency_note({"id": "p"}, "EU_STORAGE_PROCESSING")
    assert ok is False
    assert "neither GLOBAL nor" in detail
    # No claim, no finding.
    assert residency_note({"id": "p"}, None) == (True, None)


def test_the_repair_body_uses_retention_type_and_says_it_is_a_request():
    lines = repair_lines("no-retention-control", {"id": "proj_ingest"}, "zdr")
    assert any('{"retention_type": "zero_data_retention"}' in line for line in lines)
    assert any("the response field is type" in line for line in lines)
    assert any("Request it" in line for line in lines)
    assert repair_lines("compliant", {"id": "proj_ingest"}, "zdr") == []
