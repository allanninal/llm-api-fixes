from anthropic_code_execution_hours_audit import (amount, billed_hours,
                                                  code_execution_spend,
                                                  executions_ceiling, fold,
                                                  usage_report_mentions_code_execution,
                                                  verdict)


def cost(workspace="wrkspc_01Qy", cost_type="code_execution", value="84.60"):
    """One bucket of GET /v1/organizations/cost_report."""
    return {"starting_at": "2026-08-01T00:00:00Z",
            "results": [{"workspace_id": workspace, "cost_type": cost_type,
                         "description": "Code Execution Usage",
                         "amount": value, "currency": "USD"}]}


def usage_page():
    """One page of the messages usage report, as rich as it actually gets."""
    return {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": [{
        "uncached_input_tokens": 900000, "output_tokens": 40000,
        "cache_read_input_tokens": 120000,
        "cache_creation": {"ephemeral_5m_input_tokens": 30000,
                           "ephemeral_1h_input_tokens": 0},
        "server_tool_use": {"web_search_requests": 200},
        "model": "claude-sonnet-5", "api_key_id": "apikey_01Rs",
    }]}], "has_more": False}


def test_any_non_zero_amount_means_the_allowance_is_already_gone():
    # No threshold to argue about: the platform consumed the free 1,550 hours
    # before it wrote this row, so sixty cents is a finding.
    state, detail = verdict(0.60)
    assert state == "allowance-just-crossed"
    assert "12 container hour(s) on top of the free 1550" in detail
    assert verdict(0.0)[0] == "within-allowance"


def test_the_states_scale_with_how_far_past_the_allowance_you_are():
    assert verdict(40.00)[0] == "allowance-spent"
    state, detail = verdict(84.60)
    assert state == "allowance-dwarfed"
    assert "1692 container hour(s)" in detail


def test_the_usage_report_cannot_see_this_line_at_all():
    # The spine of the note. A usage result carrying tokens, cache fields and a
    # server_tool_use object still has nothing about code execution on it.
    assert usage_report_mentions_code_execution([usage_page()]) is False
    # And the check would notice the day a field did appear.
    future = {"data": [{"results": [{"code_execution_container_hours": 12}]}]}
    assert usage_report_mentions_code_execution([future]) is True


def test_amount_is_a_decimal_string_and_folds_by_workspace_and_type():
    assert amount({"amount": "84.60"}) == 84.60
    assert amount({"amount": ""}) == 0.0
    assert amount({}) == 0.0
    folded = fold([cost(value="80.00"), cost(value="4.60"),
                   cost(cost_type="web_search", value="500.00"),
                   cost(workspace="wrkspc_02Zz", cost_type="tokens", value="9.00")])
    assert folded["wrkspc_01Qy"]["code_execution"] == 84.60
    # Every cost_type is kept, not only the one being looked for.
    assert folded["wrkspc_01Qy"]["web_search"] == 500.00
    assert code_execution_spend(folded) == {"wrkspc_01Qy": 84.60}


def test_hours_are_derived_from_the_published_rate():
    assert billed_hours(84.60) == 1692.0
    assert billed_hours(0.0) == 0.0
    assert billed_hours(0.60) == 12.0


def test_the_execution_figure_is_a_ceiling_and_not_a_count():
    # Twelve billed hours is one long job or 144 short ones, and never more.
    assert executions_ceiling(12) == 144
    assert executions_ceiling(0) == 0
