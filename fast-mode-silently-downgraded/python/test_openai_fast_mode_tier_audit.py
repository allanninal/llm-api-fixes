from openai_fast_mode_tier_audit import overrides, split_spend, tier_of, verdict


def cost(project="proj_a", line_item="gpt-5.6-sol, input", value=0.0):
    return {"project_id": project, "line_item": line_item,
            "amount": {"value": value, "currency": "usd"}}


def buckets(*results):
    return [{"start_time": 0, "end_time": 86400, "results": list(results)}]


def test_configured_fast_with_standard_spend_is_a_downgrade():
    # The whole note. Nothing errored, the tier was requested, and the invoice
    # says every request in the window was served on the default tier.
    state, detail = verdict("fast", premium=0.0, standard=420.0)
    assert state == "downgraded"
    assert "not one dollar" in detail
    assert "default tier" in detail


def test_configured_standard_with_premium_spend_is_the_opposite_finding():
    state, detail = verdict("standard", premium=300.0, standard=100.0)
    assert state == "unrequested-premium"
    assert "a code path is sending the tier" in detail
    assert "2.0x" in detail


def test_a_delivered_premium_is_not_reported_as_a_failure():
    state, detail = verdict("fast", premium=380.0, standard=20.0)
    assert state == "premium-delivered"
    assert "95%" in detail


def test_a_partial_downgrade_is_its_own_state():
    state, detail = verdict("fast", premium=100.0, standard=300.0)
    assert state == "partly-downgraded"
    assert "only 25%" in detail


def test_a_missing_tier_field_is_never_read_as_standard():
    assert tier_of({"id": "proj_a", "name": "web"}) is None
    assert tier_of({"id": "proj_a", "service_tier": "  Fast "}) == "fast"
    assert tier_of({"id": "proj_a", "settings": {"service_tier": "priority"}}) == "priority"
    assert tier_of({"id": "proj_a", "settings": "fast"}) is None
    assert verdict(None, premium=0.0, standard=99.0)[0] == "unknown-tier"
    assert verdict(None, premium=50.0, standard=49.0)[0] == "unknown-tier-premium"


def test_a_project_with_no_spend_is_not_evidence_of_anything():
    assert verdict("fast", premium=0.0, standard=0.0)[0] == "no-spend"
    assert verdict("standard", premium=0.2, standard=0.1)[0] == "no-spend"


def test_premium_line_items_are_matched_by_label_and_the_labels_come_back():
    rows = buckets(
        cost(line_item="gpt-5.6-sol, input", value=100.0),
        cost(line_item="gpt-5.6-sol, input (fast)", value=40.0),
        cost(line_item="gpt-5.6-sol, priority output", value=10.0),
        cost(project="proj_b", line_item="gpt-5.6-sol, input (fast)", value=999.0),
    )
    premium, standard, labels = split_spend(rows, "proj_a")
    assert premium == 50.0
    assert standard == 100.0
    assert labels == ["gpt-5.6-sol, input (fast)", "gpt-5.6-sol, priority output"]


def test_tier_overrides_are_parsed_and_junk_is_dropped():
    assert overrides(["proj_a=Fast", "proj_b = standard "]) == {
        "proj_a": "fast", "proj_b": "standard"}
    assert overrides(["nonsense", "=fast", "proj_c="]) == {}
    assert overrides(None) == {}
