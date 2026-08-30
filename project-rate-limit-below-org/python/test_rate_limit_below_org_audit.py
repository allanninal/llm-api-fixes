from rate_limit_below_org_audit import (grade_override, group_label,
                                        inherited_limiters, limits_of, num,
                                        openai_matrix, openai_outliers,
                                        org_index, overrides_of, repair_lines,
                                        verdict)


def org_group(models, **limits):
    return {"type": "rate_limit", "group_type": "model_group",
            "models": list(models),
            "limits": [{"type": t, "value": v} for t, v in limits.items()]}


def ws_group(models, limits):
    return {"type": "workspace_rate_limit", "group_type": "model_group",
            "models": list(models), "limits": limits}


def test_a_workspace_capped_at_a_fraction_of_the_org_is_the_finding():
    # The whole note. Both numbers arrive on the same object, so no second
    # lookup can go wrong and no arithmetic is being trusted to a dashboard.
    entry = ws_group(["claude-opus-5"], [
        {"type": "input_tokens_per_minute", "value": 500_000, "org_limit": 10_000_000},
        {"type": "requests_per_minute", "value": 1_000, "org_limit": 4_000},
    ])
    rows = overrides_of(entry)
    assert [r[0] for r in rows] == ["requests_per_minute", "input_tokens_per_minute"]
    state, detail = grade_override(500_000, 10_000_000)
    assert state == "throttled-below-org"
    assert "500,000 of 10,000,000 (5%)" == detail
    assert verdict([s for s, _ in (grade_override(v, o) for _, v, o in rows)]) \
        == "throttled-below-org"
    assert any("Rate limits tab" in line for line in repair_lines(state))


def test_an_override_equal_to_the_org_value_is_a_pin_not_a_no_op():
    # A ratio check passes this at 1.0 and says nothing. It is the state where
    # a container has quietly opted out of every future tier increase.
    state, detail = grade_override(10_000_000, 10_000_000)
    assert state == "override-pinned-at-org"
    assert "will not follow the next increase" in detail
    assert any("Delete the override" in line for line in repair_lines(state))
    # And above the org value is a third thing again: it simply does not apply.
    above, above_detail = grade_override(20_000_000, 10_000_000)
    assert above == "override-above-org"
    assert "applies anyway" in above_detail


def test_a_null_org_limit_is_unjudgeable_and_never_becomes_zero():
    assert num(None) is None and num("nope") is None and num(True) is None
    state, detail = grade_override(500_000, None)
    assert state == "org-limit-unknown"
    assert "cannot be graded" in detail
    assert any("/v1/organizations/rate_limits" in line for line in repair_lines(state))
    # A zero override is the opposite: a real number, and the worst one.
    assert grade_override(0, 10_000_000)[0] == "throttled-below-org"


def test_limiters_absent_from_an_overridden_group_are_reported_as_inherited():
    org = org_index([{"data": [org_group(["claude-opus-5"],
                                         requests_per_minute=4_000,
                                         input_tokens_per_minute=10_000_000,
                                         output_tokens_per_minute=2_000_000)]}])
    label = group_label(org_group(["claude-opus-5"]))
    entry = ws_group(["claude-opus-5"], [
        {"type": "input_tokens_per_minute", "value": 500_000, "org_limit": 10_000_000},
        {"type": "requests_per_minute", "value": 1_000, "org_limit": 4_000},
    ])
    assert inherited_limiters(entry, org[label]) == [
        ("output_tokens_per_minute", 2_000_000)]
    assert verdict(["override-in-range", "limiter-inherited"]) == "limiter-inherited"


def test_the_two_endpoints_label_the_same_group_identically():
    # The join is by label, so this is the assertion the whole Anthropic side
    # rests on: a workspace entry and an organization entry for the same group
    # must produce the same key even though their type fields differ.
    models = ["claude-opus-4-8", "claude-opus-4-5"]
    assert group_label(org_group(models)) == group_label(ws_group(models, []))
    assert group_label(org_group(models)) == "model_group:claude-opus-4-5 +1"
    assert group_label({"group_type": "batch", "models": None}) == "batch"
    assert group_label(None) == "unknown_group"
    assert limits_of({"limits": [{"type": "x", "value": "not-a-number"}]}) == {}


def test_openai_needs_a_peer_because_the_object_has_no_org_value():
    one = openai_matrix({"proj_a": [
        {"model": "gpt-5.6", "max_requests_per_1_minute": 60,
         "max_tokens_per_1_minute": 150_000}]})
    assert openai_outliers(one) == []
    both = openai_matrix({
        "proj_a": [{"model": "gpt-5.6", "max_requests_per_1_minute": 10_000,
                    "max_tokens_per_1_minute": 2_000_000}],
        "proj_b": [{"model": "gpt-5.6", "max_requests_per_1_minute": 9_000,
                    "max_tokens_per_1_minute": 150_000},
                   {"model": "", "max_tokens_per_1_minute": 1}],
    })
    assert sorted(both["gpt-5.6"]) == ["proj_a", "proj_b"]
    assert "" not in both
    rows = openai_outliers(both)
    assert rows == [("gpt-5.6", "proj_b", "tpm", 150_000, 2_000_000)]
    assert any("proxy for the tier" in line
               for line in repair_lines("project-outlier"))


def test_empty_and_absent_inputs_do_not_raise():
    assert org_index(None) == {} and overrides_of(None) == []
    assert openai_matrix(None) == {} and openai_outliers(None) == []
    assert inherited_limiters(None, None) == []
    assert verdict([]) == "no-override" and verdict(None) == "no-override"
    assert grade_override(None, 10)[0] == "no-override"
    assert repair_lines("no-override") == []
