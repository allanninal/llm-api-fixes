from openai_batch_discount_audit import (accumulate, concentration, saving,
                                           sync_cost, verdict)


def bucket(*results):
    return {"start_time": 0, "end_time": 3600, "results": list(results)}


def result(project="proj_a", model="gpt-5.6-terra", batch=False, made=0,
           input_tokens=0, output_tokens=0):
    return {"project_id": project, "model": model, "batch": batch,
            "num_model_requests": made, "input_tokens": input_tokens,
            "output_tokens": output_tokens}


def test_idle_hours_stay_in_the_denominator():
    # Four buckets, one of them busy. If the empty hours were dropped the
    # workload would look perfectly flat instead of perfectly spiky.
    buckets = [bucket(result(made=0)), bucket(result(made=4000)),
               bucket(), bucket(result(made=0))]
    rows = accumulate(buckets)
    row = rows["proj_a / gpt-5.6-terra"]
    assert row["hourly"] == [0, 4000, 0, 0]
    assert row["sync_requests"] == 4000


def test_batch_and_synchronous_traffic_are_kept_apart():
    rows = accumulate([bucket(result(made=100, input_tokens=50, batch=False),
                              result(made=900, batch=True))])
    row = rows["proj_a / gpt-5.6-terra"]
    assert row["sync_requests"] == 100
    assert row["batch_requests"] == 900
    assert row["sync_input"] == 50
    assert row["hourly"] == [100]


def test_concentration_separates_a_schedule_from_an_audience():
    spiky = [0] * 18 + [4000, 1000]
    assert concentration(spiky, 0.10) == 1.0
    assert concentration([250] * 20, 0.10) == 0.1
    assert concentration([], 0.10) is None
    assert concentration([0, 0, 0], 0.10) is None


def test_a_nightly_job_on_the_synchronous_endpoint_is_the_finding():
    row = {"sync_requests": 5000, "batch_requests": 0,
           "hourly": [0] * 18 + [4000, 1000]}
    state, detail = verdict(row)
    assert state == "batch-shaped"
    assert "100% of 5000 synchronous request(s)" in detail
    assert "paying interactive prices" in detail


def test_spread_out_traffic_is_correctly_synchronous():
    row = {"sync_requests": 5000, "batch_requests": 0, "hourly": [250] * 20}
    state, detail = verdict(row)
    assert state == "interactive"
    assert "right one" in detail


def test_the_three_answers_that_are_not_findings():
    assert verdict({"sync_requests": 10, "batch_requests": 0,
                    "hourly": [10]})[0] == "too-little-traffic"
    assert verdict({"sync_requests": 100, "batch_requests": 9900,
                    "hourly": [100]})[0] == "already-batched"
    assert verdict({"sync_requests": 5000, "batch_requests": 0,
                    "hourly": []})[0] == "unmeasurable"


def test_the_money_comes_from_the_cost_report_not_a_price_table():
    costs = [{"results": [
        {"project_id": "proj_a", "line_item": "gpt-5.6-terra, input",
         "amount": {"value": 300.0, "currency": "usd"}},
        {"project_id": "proj_a", "line_item": "gpt-5.6-terra, batch input",
         "amount": {"value": 40.0, "currency": "usd"}},
        {"project_id": "proj_b", "line_item": "gpt-5.6-terra, input",
         "amount": {"value": 99.0, "currency": "usd"}},
    ]}]
    assert sync_cost(costs, "proj_a") == 300.0
    assert sync_cost(costs) == 399.0
    assert saving(300.0) == 150.0
    assert saving(0) == 0.0
    assert saving(None) is None
