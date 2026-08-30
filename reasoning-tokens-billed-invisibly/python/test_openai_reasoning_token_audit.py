import datetime as dt

from openai_reasoning_token_audit import split, totals, verdict

NOW = dt.datetime(2026, 8, 30, 0, 0, tzinfo=dt.timezone.utc)


def days_ago(d):
    return int(NOW.timestamp() - d * 86400)


def day(d, requests=100, inp=90000, out=100000, model="gpt-5.6"):
    return {"start_time": days_ago(d),
            "results": [{"model": model, "num_model_requests": requests,
                         "input_tokens": inp, "output_tokens": out}]}


def anthropic_day(d, inp=90000, out=100000):
    """No num_model_requests: that field does not exist on Anthropic's report."""
    return {"start_time": days_ago(d),
            "results": [{"uncached_input_tokens": inp, "output_tokens": out}]}


def test_totals_sums_and_tolerates_a_missing_request_count():
    assert totals([day(1), day(2)]) == {"requests": 200, "input": 180000,
                                        "output": 200000, "buckets": 2}
    assert totals([anthropic_day(1)])["requests"] == 0


def test_split_cuts_the_series_at_the_clock_it_is_given():
    prior, recent = split([day(1), day(3), day(9), day(30)], NOW, 7)
    assert [b["start_time"] for b in recent] == [days_ago(1), days_ago(3)]
    assert [b["start_time"] for b in prior] == [days_ago(9)]
    # 30 days back is outside twice the window and is dropped, not compared.


def test_the_finding_output_per_request_rises_while_input_holds():
    prior = [day(9, requests=100, inp=90000, out=100000)]
    recent = [day(1, requests=100, inp=91000, out=400000)]
    state, detail = verdict(prior, recent)
    assert state == "reasoning-tax"
    assert "4.0x" in detail
    assert "never returned" in detail


def test_prompts_growing_is_not_the_same_finding():
    prior = [day(9, requests=100, inp=90000, out=100000)]
    recent = [day(1, requests=100, inp=360000, out=400000)]
    assert verdict(prior, recent)[0] == "longer-prompts"


def test_more_traffic_at_the_same_ratios_is_not_a_finding_at_all():
    prior = [day(9, requests=100, inp=90000, out=100000)]
    recent = [day(1, requests=400, inp=360000, out=400000)]
    state, detail = verdict(prior, recent)
    assert state == "volume-only"
    assert "unit economics" in detail


def test_flat_ratios_and_flat_traffic_are_steady():
    prior = [day(9, requests=100, inp=90000, out=100000)]
    recent = [day(1, requests=110, inp=99000, out=110000)]
    assert verdict(prior, recent)[0] == "steady"


def test_no_request_count_degrades_to_a_weaker_claim_and_says_so():
    prior = [anthropic_day(9, inp=90000, out=100000)]
    recent = [anthropic_day(1, inp=90000, out=400000)]
    state, detail = verdict(prior, recent)
    assert state == "unmeasurable-but-rising"
    assert "per input token, not per request" in detail
    assert verdict([anthropic_day(9)], [anthropic_day(1)])[0] == "unmeasurable"


def test_requests_with_no_output_is_an_error_shape_not_a_reasoning_one():
    prior = [day(9)]
    recent = [day(1, requests=50, inp=45000, out=0)]
    state, _ = verdict(prior, recent)
    assert state == "failing-before-generation"


def test_an_empty_recent_window_claims_nothing():
    assert verdict([day(9)], [])[0] == "no-data"
