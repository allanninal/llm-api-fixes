import datetime as dt

from llm_idle_key_audit import (age_days, anthropic_verdict, audit_gaps,
                                openai_verdict, repair_lines, revocation_order,
                                safe_hint, seen_key_ids)

NOW = dt.datetime(2026, 8, 31, 12, 0, 0, tzinfo=dt.timezone.utc)


def unix(days_ago):
    return int((NOW - dt.timedelta(days=days_ago)).timestamp())


def test_a_key_whose_owner_is_perfectly_fine_is_still_the_finding():
    # The note in one assertion, and the line that keeps it away from the
    # published offboarding note. This owner is present, active and employed.
    # The verdict does not read the owner at all.
    key = {"id": "key_a1", "name": "vendor-trial",
           "redacted_value": "sk-...4f7a", "created_at": unix(154),
           "last_used_at": None,
           "owner": {"type": "user", "user": {"email": "dev@example.test"}},
           "owner_project_access": "active"}
    state, detail = openai_verdict(key, NOW)
    assert state == "never-used"
    assert "154 day(s)" in detail
    assert any("cannot break traffic" in line
               for line in repair_lines(state, {"container": "proj_1",
                                                "id": "key_a1"}))


def test_the_two_providers_answer_different_strengths_of_the_question():
    # OpenAI reads a field and may say "never". Anthropic reads a set
    # difference over a window and must not.
    openai_state, openai_detail = openai_verdict(
        {"created_at": unix(200), "last_used_at": None}, NOW)
    assert openai_state == "never-used"
    assert "never used" in openai_detail

    anthropic_state, anthropic_detail = anthropic_verdict(
        {"id": "apikey_z9", "status": "active",
         "created_at": "2025-01-04T09:12:00Z"}, set(), 30, NOW)
    assert anthropic_state == "unused-in-window"
    assert "last 30 day(s)" in anthropic_detail
    assert "no last_used_at field" in anthropic_detail
    assert "never used" not in anthropic_detail.split("not a claim")[0]


def test_the_two_defaulted_parameters_are_the_audit_and_are_asserted():
    assert audit_gaps({"include_archived": "true"},
                      {"owner_project_access": "any"}) == []
    gaps = audit_gaps({"limit": 100}, {"limit": 100})
    assert len(gaps) == 2
    assert any("include_archived" in g for g in gaps)
    assert any("owner_project_access" in g for g in gaps)
    # The dangerous middle case: one parameter remembered, one forgotten.
    assert len(audit_gaps({"include_archived": "true"}, {"limit": 100})) == 1


def test_dates_arrive_in_two_shapes_and_a_zero_is_not_1970():
    assert age_days(unix(45), NOW) == 45
    assert age_days("2026-08-01T00:00:00Z", NOW) == 30
    assert age_days("2026-08-01T00:00:00+00:00", NOW) == 30
    assert age_days(str(unix(7)), NOW) == 7
    assert age_days(None, NOW) is None
    assert age_days("not a date", NOW) is None
    assert age_days(True, NOW) is None
    # last_used_at of 0 is absent, not a use in 1970.
    state, _ = openai_verdict({"created_at": unix(100), "last_used_at": 0}, NOW)
    assert state == "never-used"


def test_dormant_and_never_used_are_graded_and_ordered_apart():
    fresh = openai_verdict({"created_at": unix(120), "last_used_at": unix(3)}, NOW)
    assert fresh[0] == "in-use"
    old = openai_verdict({"created_at": unix(900), "last_used_at": unix(412)}, NOW)
    assert old[0] == "dormant"
    assert "412 day(s) ago" in old[1]
    young = openai_verdict({"created_at": unix(9), "last_used_at": None}, NOW)
    assert young[0] == "too-new"

    order = revocation_order([
        {"state": "dormant", "idle": 412, "name": "nightly"},
        {"state": "in-use", "idle": 1, "name": "prod"},
        {"state": "never-used", "idle": 154, "name": "vendor-trial"},
        {"state": "unused-in-window", "idle": 300, "name": "ingest"},
    ])
    assert [r["state"] for r in order] == ["never-used", "unused-in-window",
                                           "dormant"]


def test_an_anthropic_key_seen_in_the_report_is_not_a_finding():
    pages = [{"data": [{"results": [{"api_key_id": "apikey_a"},
                                    {"api_key_id": None},
                                    {"api_key_id": "apikey_b"}]}]}]
    seen = seen_key_ids(pages)
    assert seen == {"apikey_a", "apikey_b"}
    assert anthropic_verdict({"id": "apikey_a", "status": "active",
                              "created_at": "2024-02-02T00:00:00Z"},
                             seen, 30, NOW)[0] == "seen-in-window"
    assert anthropic_verdict({"id": "apikey_c", "status": "archived"},
                             seen, 30, NOW)[0] == "not-active"
    assert seen_key_ids([]) == set()
    assert seen_key_ids(None) == set()


def test_no_key_value_can_reach_the_output():
    assert safe_hint("sk-...4f7a") == "sk-...4f7a"
    assert safe_hint("sk-ant-...igAA") == "sk-ant-...igAA"
    assert safe_hint("sk-abcd****wxyz") == "sk-abcd****wxyz"
    # Anything that is not already redacted is refused, whatever it is.
    assert safe_hint("sk-fake-not-redacted-value") == "(hint withheld)"
    assert safe_hint("...." + "x" * 60) == "(hint withheld)"
    assert safe_hint(None) == "(no hint)"
    assert safe_hint("") == "(no hint)"


def test_the_repairs_say_different_things_for_the_two_findings():
    never = repair_lines("never-used", {"container": "proj_1", "id": "key_1"})
    dormant = repair_lines("dormant", {})
    window = repair_lines("unused-in-window", {})
    assert any("safest credentials" in line for line in never)
    assert any("Ask what it was before revoking" in line for line in dormant)
    assert any("not proven unused" in line for line in window)
    assert repair_lines("in-use", {}) == []
