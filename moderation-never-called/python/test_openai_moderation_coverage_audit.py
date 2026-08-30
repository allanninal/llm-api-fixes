from openai_moderation_coverage_audit import (classify, coverage, fold,
                                              is_retired, repair_lines,
                                              retired_ids)

COMPLETION = "organization.usage.completions.result"
MODERATION = "organization.usage.moderations.result"


def bucket(*results):
    return {"object": "bucket", "start_time": 0, "end_time": 86400,
            "results": list(results)}


def row(project, model, n, obj=MODERATION):
    return {"object": obj, "project_id": project, "model": model,
            "num_model_requests": n, "input_tokens": n * 12}


def test_a_busy_project_with_no_moderations_entry_survives_the_join():
    # The note. proj_public never appears in the moderations report at all, so
    # a join driven by the moderation side would report nothing wrong.
    completions = fold([bucket(row("proj_public", "gpt-4.1-mini", 20604, COMPLETION),
                               row("proj_intake", "gpt-4.1", 2000, COMPLETION)),
                        bucket(row("proj_public", "gpt-4.1-mini", 20604, COMPLETION))])
    moderations = fold([bucket(row("proj_intake", "omni-moderation-latest", 1900))])

    rows = coverage(completions, moderations)
    assert [r[0] for r in rows] == ["proj_public", "proj_intake"]

    state, detail = classify(rows[0])
    assert state == "never-called"
    assert "41208 completion request(s)" in detail
    lines = repair_lines(state, rows[0])
    assert any("bills nothing" in line for line in lines)
    assert any("category_scores" in line for line in lines)

    assert classify(rows[1])[0] == "covered"


def test_full_coverage_on_a_retired_id_is_a_finding_not_coverage():
    # Every request moderated, ratio near 1.0, and still wrong. A count-based
    # audit reports this project as healthy.
    completions = fold([bucket(row("proj_old", "gpt-4.1", 4000, COMPLETION))])
    moderations = fold([bucket(row("proj_old", "text-moderation-latest", 3904))])
    rows = coverage(completions, moderations)

    state, detail = classify(rows[0])
    assert state == "retired-model-id"
    assert "100% of them on text-moderation-latest" in detail
    lines = repair_lines(state, rows[0])
    assert any("omni-moderation-latest" in line for line in lines)
    assert any("images" in line for line in lines)


def test_a_pinned_snapshot_is_caught_and_the_current_id_is_not():
    assert is_retired("text-moderation-007") is True
    assert is_retired("text-moderation-stable") is True
    assert is_retired("TEXT-MODERATION-LATEST") is True
    assert is_retired("omni-moderation-latest") is False
    assert is_retired("omni-moderation-2024-09-26") is False
    assert is_retired(None) is False
    assert retired_ids({"omni-moderation-latest": 5, "text-moderation-007": 2}) == \
        ["text-moderation-007"]
    # A part-migrated project is still a finding, and both ids are named.
    mixed = ("proj_half", 4000, 3900,
             {"omni-moderation-latest": 3000, "text-moderation-007": 900})
    state, detail = classify(mixed)
    assert state == "retired-model-id"
    assert "23% of them" in detail


def test_a_low_volume_project_is_never_graded():
    quiet = ("proj_scratch", 41, 0, {})
    state, detail = classify(quiet)
    assert state == "below-floor"
    assert "under the 500 floor" in detail
    assert repair_lines(state, quiet) == []
    # And it becomes gradeable once the floor is lowered deliberately.
    assert classify(quiet, min_completions=10)[0] == "never-called"


def test_a_zero_valued_result_row_creates_no_entry():
    # A project present in the report with a zero count must not look moderated.
    moderations = fold([bucket(row("proj_a", "omni-moderation-latest", 0),
                               row("proj_b", "omni-moderation-latest", 7))])
    assert "proj_a" not in moderations
    assert moderations["proj_b"]["requests"] == 7
    assert fold(None) == {}
    assert fold([{"results": None}]) == {}
    assert fold([bucket({"num_model_requests": "not a number"})]) == {}
    # A result with no project_id is kept under an explicit name, not dropped.
    assert "unattributed" in fold([bucket({"num_model_requests": 3})])


def test_the_ratio_is_graded_as_the_soft_signal_it_is():
    thin = ("proj_thin", 10000, 400, {"omni-moderation-latest": 400})
    state, detail = classify(thin)
    assert state == "thin-coverage"
    assert "ratio of 0.04" in detail
    assert any("cannot tell you which" in line for line in repair_lines(state, thin))
    assert classify(thin, min_ratio=0.01)[0] == "covered"
    assert repair_lines("covered", thin) == []
