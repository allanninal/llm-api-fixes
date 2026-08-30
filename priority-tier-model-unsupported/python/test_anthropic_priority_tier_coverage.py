from anthropic_priority_tier_coverage import (fold, is_unsupported,
                                              org_has_priority, repair_lines,
                                              share, tier, verdict, weigh)


def result(model, service_tier, tokens):
    return {"model": model, "service_tier": service_tier,
            "uncached_input_tokens": tokens}


def page(results):
    return {"data": [{"results": results}], "has_more": False}


COVERED_ORG = [page([
    result("claude-opus-5", "standard", 812_400_000),
    result("claude-opus-4-5", "priority", 41_800_000),
    result("claude-opus-4-5", "standard", 4_100_000),
])]


def test_a_model_that_never_reports_priority_has_no_coverage():
    # The note in one assertion. The org clearly has a commitment, because
    # another model is being served on it, so this model's clean zero is a
    # fact about the model rather than about the organization.
    rows = fold(COVERED_ORG)
    assert org_has_priority(rows) is True
    state, detail = verdict("claude-opus-5", rows["claude-opus-5"], True)
    assert state == "unsupported-model"
    assert "not supported by Priority Tier" in detail
    assert verdict("claude-opus-4-5", rows["claude-opus-4-5"], True)[0] == \
        "priority-covered"
    assert any("coverage, not configuration" in line
               for line in repair_lines(state, "claude-opus-5"))


def test_an_org_with_no_commitment_is_not_a_per_model_finding():
    # Identical traffic with the priority row removed. Nothing about the model
    # changed; the correct verdict did, because there is no commitment anywhere
    # and Priority capacity can no longer be bought.
    rows = fold([page([
        result("claude-opus-5", "standard", 812_400_000),
        result("claude-opus-4-5", "standard", 45_900_000),
    ])])
    assert org_has_priority(rows) is False
    for model in rows:
        state, detail = verdict(model, rows[model], False)
        assert state == "no-priority-in-org"
        assert "without a capacity commitment" in detail
        assert repair_lines(state, model) == []


def test_the_exclusion_list_matches_families_and_not_neighbours():
    assert is_unsupported("claude-opus-5") is True
    assert is_unsupported("claude-sonnet-5-20260101") is True
    assert is_unsupported("claude-mythos-5") is True
    assert is_unsupported("claude-mythos-preview") is True
    # The ones a careless substring test destroys.
    assert is_unsupported("claude-opus-4-5") is False
    assert is_unsupported("claude-haiku-4-5-20251001") is False
    assert is_unsupported("claude-sonnet-4-6") is False
    assert is_unsupported("claude-fable-5") is False
    assert is_unsupported(None) is False


def test_a_model_off_the_list_with_zero_priority_is_a_different_finding():
    rows = fold([page([
        result("claude-haiku-4-5-20251001", "standard", 240_000_000),
        result("claude-opus-4-5", "priority", 40_000_000),
    ])])
    state, detail = verdict("claude-haiku-4-5-20251001",
                            rows["claude-haiku-4-5-20251001"], True)
    assert state == "uncovered-model"
    assert "not on the documented exclusion list" in detail


def test_a_thin_priority_share_is_a_sizing_finding():
    rows = fold([page([
        result("claude-haiku-4-5-20251001", "priority", 14_000_000),
        result("claude-haiku-4-5-20251001", "standard", 86_000_000),
    ])])
    state, detail = verdict("claude-haiku-4-5-20251001",
                            rows["claude-haiku-4-5-20251001"], True)
    assert state == "partial-priority"
    assert "14% priority" in detail
    assert any("burndown" in line for line in
               repair_lines(state, "claude-haiku-4-5-20251001"))


def test_cache_creation_is_an_object_and_all_of_it_counts():
    row = {"uncached_input_tokens": 100, "cache_read_input_tokens": 10,
           "output_tokens": 5,
           "cache_creation": {"ephemeral_5m_input_tokens": 40,
                              "ephemeral_1h_input_tokens": 20}}
    assert weigh(row) == 175
    assert weigh({"uncached_input_tokens": "not a number"}) == 0
    assert weigh({"cache_creation": 12}) == 0
    assert weigh(None) == 0


def test_an_absent_service_tier_never_lands_in_standard():
    assert tier({"service_tier": "priority"}) == "priority"
    assert tier({"service_tier": "BATCH"}) == "batch"
    assert tier({}) == "unknown"
    assert tier({"service_tier": "flex"}) == "unknown"
    rows = fold([page([result("claude-opus-5", None, 5_000_000)])])
    assert rows["claude-opus-5"]["standard"] == 0
    assert rows["claude-opus-5"]["unknown"] == 5_000_000
    assert share(rows["claude-opus-5"], "standard") == 0.0


def test_too_little_traffic_is_never_a_verdict():
    rows = fold([page([result("claude-opus-5", "standard", 900)])])
    state, detail = verdict("claude-opus-5", rows["claude-opus-5"], True)
    assert state == "low-volume"
    assert "too few to conclude" in detail
    assert fold([]) == {} and fold(None) == {}
    assert org_has_priority({}) is False
