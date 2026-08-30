from anthropic_web_search_spend_audit import (fee, fold, reconcile,
                                              search_spend, verdict)


def page(*results):
    """One page of GET /v1/organizations/usage_report/messages."""
    return {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": list(results)}],
            "has_more": False}


def usage(key="apikey_01Rs", searches=None, **tools):
    """One usage result. server_tool_use is nested beside the token fields."""
    use = dict(tools)
    if searches is not None:
        use["web_search_requests"] = searches
    row = {"api_key_id": key, "uncached_input_tokens": 900000,
           "output_tokens": 40000}
    if use:
        row["server_tool_use"] = use
    return row


def cost(cost_type="web_search", amount="1174.40"):
    """One bucket of GET /v1/organizations/cost_report."""
    return {"starting_at": "2026-08-01T00:00:00Z",
            "results": [{"cost_type": cost_type, "amount": amount,
                         "currency": "USD"}]}


def test_the_counter_is_nested_and_a_flat_read_finds_nothing():
    # The whole note. Both results carry a five figure search count, and a fold
    # that only looks at top-level fields reports this key as never searching.
    rows = fold([page(usage(searches=60000), usage(searches=58400))])
    assert rows["apikey_01Rs"]["web_search"] == 118400
    # A result with no server_tool_use at all is still a key, with zero searches.
    assert fold([page(usage())])["apikey_01Rs"]["web_search"] == 0


def test_the_fee_is_per_thousand_searches_not_per_search():
    assert fee(118400) == 1184.00
    assert fee(1) == 0.01
    assert fee(0) == 0.0
    assert fee(None) == 0.0


def test_a_high_volume_key_is_the_finding_and_quotes_the_fee():
    state, detail = verdict({"web_search": 118400, "other_tools": {}})
    assert state == "search-fee"
    assert "tool fee of about $1184.00" in detail


def test_a_handful_of_searches_is_a_demo_and_not_a_bill():
    assert verdict({"web_search": 12, "other_tools": {}})[0] == "low-volume"
    assert verdict({"web_search": 0, "other_tools": {}})[0] == "no-searches"
    assert verdict({})[0] == "no-searches"


def test_an_unknown_server_tool_counter_stays_visible():
    # A counter this script was not written for must not be silently dropped,
    # or the next billable server tool arrives and nothing changes on screen.
    rows = fold([page(usage(searches=200, web_fetch_requests=90,
                            code_execution_sessions=0))])
    row = rows["apikey_01Rs"]
    assert row["web_search"] == 200
    assert row["other_tools"] == {"web_fetch_requests": 90}


def test_only_the_web_search_cost_type_counts_and_amount_is_a_string():
    buckets = [cost("web_search", "1174.40"),
               cost("web_search", "10.00"),
               cost("code_execution", "500.00"),
               cost("web_search", "")]
    assert search_spend(buckets) == 1184.40
    assert search_spend([]) == 0.0


def test_the_four_ways_the_two_reports_can_disagree_stay_four_answers():
    assert reconcile(1184.00, 1174.40)[0] == "confirmed"
    # Counted but not billed: errored searches are free, and the report lags.
    assert reconcile(1184.00, 0.0)[0] == "unpriced"
    # Billed but not counted: the two windows do not line up.
    assert reconcile(0.0, 1174.40)[0] == "billed-without-count"
    state, detail = reconcile(100.00, 900.00)
    assert state == "mismatch"
    assert "800% apart" in detail
    assert reconcile(0.0, 0.0)[0] == "no-searches"
