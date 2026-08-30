from openai_streaming_usage_gap import (api_totals, compare, recorded_tokens,
                                          untracked_cost)


def bucket(*results):
    return {"start_time": 0, "end_time": 86400, "results": list(results)}


def usage(project="proj_chat", input_tokens=0, output_tokens=0, requests=0):
    return {"project_id": project, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "num_model_requests": requests}


def test_a_dashboard_short_of_the_org_report_is_the_finding():
    state, detail = compare(api_tokens=42_000_000, recorded=28_000_000)
    assert state == "undercount"
    assert "short by 14000000" in detail
    assert "33.3%" in detail
    assert "usage: null" in detail


def test_recording_more_than_you_were_billed_for_is_a_different_bug():
    # An absolute-value comparison would call this an undercount and send
    # somebody to add a streaming parameter that changes nothing.
    state, detail = compare(api_tokens=10_000_000, recorded=13_000_000)
    assert state == "overcount"
    assert "double counting" in detail


def test_a_project_missing_from_telemetry_is_not_an_undercount():
    state, detail = compare(api_tokens=9_000_000, recorded=None)
    assert state == "untracked"
    assert "nothing here is being recorded" in detail
    # Recorded as zero is a different sentence: the pipeline saw it.
    assert compare(api_tokens=9_000_000, recorded=0)[0] == "undercount"


def test_tokens_recorded_against_a_project_with_no_usage_are_a_mapping_bug():
    state, detail = compare(api_tokens=0, recorded=5_000_000)
    assert state == "phantom"
    assert "project id mapping" in detail
    assert compare(api_tokens=0, recorded=None)[0] == "idle"
    assert compare(api_tokens=0, recorded=0)[0] == "idle"


def test_small_projects_and_close_numbers_are_not_findings():
    assert compare(api_tokens=5_000, recorded=1)[0] == "too-little-traffic"
    state, detail = compare(api_tokens=1_000_000, recorded=980_000)
    assert state == "matched"
    assert "2.0% apart" in detail


def test_usage_buckets_fold_into_one_row_per_project():
    rows = api_totals([
        bucket(usage(input_tokens=100, output_tokens=20, requests=3),
               usage(project="proj_batch", input_tokens=7, output_tokens=1)),
        bucket(usage(input_tokens=50, output_tokens=5, requests=2)),
    ])
    assert rows["proj_chat"] == {"tokens": 175, "requests": 5}
    assert rows["proj_batch"] == {"tokens": 8, "requests": 0}


def test_telemetry_is_read_leniently_but_absence_is_preserved():
    assert recorded_tokens(1200) == 1200
    assert recorded_tokens({"tokens": 1200}) == 1200
    assert recorded_tokens({"input_tokens": 900, "output_tokens": 300}) == 1200
    assert recorded_tokens(0) == 0
    assert recorded_tokens(None) is None
    assert recorded_tokens({}) is None
    assert recorded_tokens("lots") is None
    assert recorded_tokens(True) is None


def test_the_money_is_a_pro_rata_share_of_reported_spend():
    costs = [bucket({"project_id": "proj_chat",
                     "amount": {"value": 300.0, "currency": "usd"}},
                    {"project_id": "proj_other",
                     "amount": {"value": 900.0, "currency": "usd"}})]
    assert untracked_cost(costs, "proj_chat", 1_000_000, 250_000) == 75.0
    assert untracked_cost(costs, "proj_chat", 1_000_000, 0) == 0.0
    assert untracked_cost(costs, "proj_chat", 0, 100) == 0.0
    assert untracked_cost(costs, "proj_missing", 1_000_000, 500_000) == 0.0
