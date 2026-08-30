from fine_tuning_gate_audit import (BASE_SHUTDOWN, CUTOFF, WINDOW,
                                    create_eligibility, days_left, family_for,
                                    job_verdict, repair_lines,
                                    serving_deadline)

TODAY = "2026-08-31"


def test_a_blocked_verdict_comes_from_readable_state_and_not_an_attempt():
    # The whole constraint, made mechanical. Three inputs, all readable with
    # the keys this script already holds, and no job is ever submitted.
    state, detail = create_eligibility(TODAY, True, 63)
    assert state == "blocked-no-recent-inference"
    assert "63 day(s)" in detail
    assert "Read from usage, not from an attempt" in detail
    lines = repair_lines(state)
    assert any("route real traffic" in line for line in lines)
    assert any(BASE_SHUTDOWN in line and CUTOFF in line for line in lines)

    state, detail = create_eligibility(TODAY, False, 3)
    assert state == "blocked-never-fine-tuned"
    assert "Read from the listing, not from an attempt" in detail


def test_the_window_closing_is_its_own_state_with_the_days_left_in_it():
    state, detail = create_eligibility(TODAY, True, 52)
    assert state == "eligibility-expiring"
    assert "%d day(s)" % (WINDOW - 52) in detail
    assert create_eligibility(TODAY, True, 12)[0] == "eligible"
    assert create_eligibility("2027-02-01", True, 1)[0] == "create-closed"
    # Before the middle rule applied, recent inference was irrelevant.
    assert create_eligibility("2026-06-01", True, 400)[0] == "eligible"


def test_the_three_shapes_of_the_inference_clock():
    assert create_eligibility(TODAY, True, "none-in-window")[0] == \
        "blocked-no-recent-inference"
    state, detail = create_eligibility(TODAY, True, None)
    assert state == "unknown-eligibility"
    assert "unknown rather than fine" in detail
    assert any("admin-read key" in line for line in repair_lines(state))


def test_a_base_is_matched_exactly_or_on_a_hyphen_and_never_loosely():
    # gpt-4.1-nano-2025-04-14 starts with the characters gpt-4. Filing it under
    # ft-gpt-4 would print gpt-5.6-sol with complete confidence.
    family, replacement = family_for("gpt-4.1-nano-2025-04-14")
    assert family == "ft-gpt-4.1-nano-2025-04-14"
    assert replacement == "gpt-5.6-luna"
    assert family_for("gpt-4")[0] == "ft-gpt-4"
    assert family_for("gpt-4-0613") == ("ft-gpt-4", "gpt-5.6-sol")
    assert family_for("gpt-3.5-turbo-0125")[1] == "gpt-5.6-terra"
    # A base the table does not cover is unknown, not the nearest-looking row.
    assert family_for("gpt-4o-mini-2024-07-18") == (None, None)
    assert family_for(None) == (None, None)


def test_a_date_from_the_api_is_labelled_apart_from_the_published_table():
    date, source, why = serving_deadline("2026-12-01", "ft-gpt-4")
    assert (date, source) == ("2026-12-01", "api")
    assert "read off the model object" in why

    date, source, why = serving_deadline(None, "ft-gpt-4")
    assert (date, source) == (BASE_SHUTDOWN, "published-table")
    assert "ft-gpt-4 row in the deprecation table" in why

    date, source, _ = serving_deadline(None, None)
    assert (date, source) == (None, "unknown")


def test_the_two_verbs_are_graded_apart_and_can_disagree():
    # The pair this note exists to produce: creation already refused while a
    # fine-tune is serving perfectly well for months yet.
    create, _ = create_eligibility(TODAY, True, 63)
    serve, detail = job_verdict("succeeded", "ft:gpt-4:acme::Ab12",
                                "2027-06-01", TODAY)
    assert create == "blocked-no-recent-inference"
    assert serve == "serving"
    assert "day(s) of inference left" in detail

    state, detail = job_verdict("succeeded", "ft:gpt-4:acme::Ab12",
                                BASE_SHUTDOWN, TODAY)
    assert state == "dying-soon"
    assert "53 day(s)" in detail
    lines = repair_lines(state, "gpt-5.6-sol")
    assert any("gpt-5.6-sol" in line for line in lines)
    assert any("structured outputs" in line for line in lines)


def test_a_job_with_nothing_serving_and_a_base_with_no_date():
    assert job_verdict("failed", None, BASE_SHUTDOWN, TODAY)[0] == "not-serving"
    assert job_verdict("succeeded", None, BASE_SHUTDOWN, TODAY)[0] == "not-serving"
    state, _ = job_verdict("succeeded", "ft:x:acme::Zz99", None, TODAY)
    assert state == "no-base-date"
    assert any("undated rather than as safe" in line
               for line in repair_lines(state))
    assert job_verdict("succeeded", "ft:x:acme::Zz99", "2026-08-01",
                       TODAY)[0] == "already-dead"
    assert days_left(TODAY, BASE_SHUTDOWN) == 53
