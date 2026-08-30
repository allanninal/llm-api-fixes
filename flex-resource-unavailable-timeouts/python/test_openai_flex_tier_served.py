from openai_flex_tier_served import (flex_by_hour, flex_gaps, hours_active,
                                     median, never_served, stamp, repair_lines,
                                     tier_rows, tiers_for_model, totals_by_tier,
                                     verdict)

HOUR = 3600
BASE = 1787000000 // HOUR * HOUR


def result(model, tier, requests, out=0):
    return {"object": "organization.usage.completions.result",
            "input_tokens": requests * 800, "output_tokens": out,
            "num_model_requests": requests, "project_id": None,
            "model": model, "batch": False, "service_tier": tier}


def week(flex_per_hour, other_per_hour, hours=24, model="gpt-5.6"):
    data = []
    for i in range(hours):
        results = []
        flex = flex_per_hour(i)
        other = other_per_hour(i)
        if flex:
            results.append(result(model, "flex", flex))
        if other:
            results.append(result(model, "default", other))
        data.append({"object": "bucket", "start_time": BASE + i * HOUR,
                     "end_time": BASE + (i + 1) * HOUR, "results": results})
    return [{"object": "page", "data": data, "has_more": False, "next_page": None}]


def test_hours_where_flex_collapsed_while_other_tiers_kept_serving():
    # The note. Flex runs at about 2,000 an hour except for three hours where
    # it is refused, and default keeps going throughout, which is what makes
    # those three hours a capacity signal rather than a quiet night.
    dead = {5, 11, 19}
    pages = week(lambda i: 0 if i in dead else 2_000, lambda i: 8_000)
    rows = tier_rows(pages)
    gaps = flex_gaps(flex_by_hour(rows, "gpt-5.6"), hours_active(rows))
    assert [g[0] for g in gaps] == [BASE + h * HOUR for h in sorted(dead)]
    assert gaps[0][1] == 0.0 and gaps[0][2] == 8_000.0 and gaps[0][3] == 2_000.0
    state, detail = verdict("gpt-5.6", flex_by_hour(rows, "gpt-5.6"), gaps,
                            tiers_for_model(rows, "gpt-5.6"), ["gpt-5.6"])
    assert state == "flex-shortfall"
    assert "3 hour(s)" in detail
    lines = repair_lines(state)
    assert any("Resource Unavailable" in line for line in lines)
    assert any("15 minutes" in line for line in lines)
    assert stamp(BASE).endswith(":00Z")


def test_a_quiet_night_is_not_a_capacity_failure():
    # The control. Same three empty flex hours, but nothing else ran either.
    dead = {5, 11, 19}
    pages = week(lambda i: 0 if i in dead else 2_000,
                 lambda i: 0 if i in dead else 8_000)
    rows = tier_rows(pages)
    assert flex_gaps(flex_by_hour(rows, "gpt-5.6"), hours_active(rows)) == []
    state, _ = verdict("gpt-5.6", flex_by_hour(rows, "gpt-5.6"), [],
                       tiers_for_model(rows, "gpt-5.6"), ["gpt-5.6"])
    assert state == "flex-served"


def test_a_model_configured_for_flex_that_never_gets_it():
    pages = week(lambda i: 0, lambda i: 1_717)
    rows = tier_rows(pages)
    tiers = tiers_for_model(rows, "gpt-5.6")
    assert tiers == {"default": 41_208.0}
    state, detail = verdict("gpt-5.6", {}, [], tiers, ["gpt-5.6"])
    assert state == "flex-never-served"
    assert "41,208 on other tiers" in detail
    assert any("rewrites request bodies" in line for line in repair_lines(state))
    # And the same model, not declared as configured, is nobody's business.
    assert verdict("gpt-5.6", {}, [], tiers, [])[0] == "no-flex-usage"
    assert never_served(rows, ["gpt-5.6"])[0][0] == "gpt-5.6"
    assert never_served(rows, []) == []
    assert never_served(rows, ["never-called-model"]) == []


def test_too_little_flex_history_declines_rather_than_grading():
    pages = week(lambda i: 2_000 if i < 4 else 0, lambda i: 8_000)
    rows = tier_rows(pages)
    flex_hours = flex_by_hour(rows, "gpt-5.6")
    assert flex_gaps(flex_hours, hours_active(rows)) == []
    state, detail = verdict("gpt-5.6", flex_hours, [],
                            tiers_for_model(rows, "gpt-5.6"), ["gpt-5.6"])
    assert state == "too-little-history"
    assert "not enough to take a median" in detail
    assert any("not a clean bill of health" in line for line in repair_lines(state))


def test_the_median_is_a_median_and_not_a_mean():
    # One enormous backfill hour. The mean of this is over 12,000, which would
    # put every ordinary hour under half of it and report the whole week.
    values = [2_000, 2_000, 2_000, 2_000, 2_000, 100_000]
    assert median(values) == 2_000.0
    assert sum(values) / len(values) > 12_000
    assert median([]) == 0.0
    assert median([5]) == 5.0
    assert median([1, 3]) == 2.0


def test_the_fold_keeps_absent_hours_absent():
    pages = week(lambda i: 0 if i % 2 else 100, lambda i: 50)
    rows = tier_rows(pages)
    flex_hours = flex_by_hour(rows, "gpt-5.6")
    # Twelve flex hours out of twenty four, and the other twelve are missing
    # rather than present with a zero.
    assert len(flex_hours) == 12
    assert all(v == 100.0 for v in flex_hours.values())
    assert len(hours_active(rows)) == 24
    assert totals_by_tier(rows) == {"flex": 1_200.0, "default": 1_200.0}
    assert tier_rows(None) == {} and totals_by_tier(None) == {}
    assert hours_active(None) == {} and flex_by_hour(None, "x") == {}
    assert verdict("x", {}, [], {}, [])[0] == "no-flex-usage"
    assert repair_lines("flex-served") == []
