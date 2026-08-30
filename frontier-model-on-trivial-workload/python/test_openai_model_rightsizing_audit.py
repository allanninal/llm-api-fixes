from openai_model_rightsizing_audit import (fold, permissions_state, sibling,
                                            spend_for, tier, verdict)


def row(requests=10000, output=190000, input_=900000, projects=("proj_a",)):
    """A folded row shaped like fold() returns them."""
    return {"requests": requests, "output": output, "input": input_,
            "projects": list(projects)}


def bucket(**results):
    """One daily bucket from GET /v1/organization/usage/completions."""
    return {"data": [{"start_time": 0, "results": [
        {"model": m, "num_model_requests": r, "input_tokens": i,
         "output_tokens": o, "project_id": p}
        for m, (r, i, o, p) in results.items()]}]}


def test_a_premium_model_with_tiny_answers_is_the_finding():
    # The whole note: 412,880 calls, mean answer 19 tokens, on the frontier model.
    state, detail = verdict("gpt-5", row(requests=412880, output=7844720,
                                         input_=170000000))
    assert state == "oversized"
    assert "mean output 19 token(s)" in detail
    assert sibling("gpt-5") == "gpt-5-mini"


def test_the_same_shape_on_the_mini_sibling_is_not_a_finding():
    state, _ = verdict("gpt-5-mini", row(requests=412880, output=7844720,
                                         input_=170000000))
    assert state == "right-sized"


def test_long_answers_are_the_model_doing_its_job():
    state, detail = verdict("gpt-5", row(requests=9000, output=18000000,
                                         input_=9000000))
    assert state == "deliberative"
    assert "mean output 2000 token(s)" in detail


def test_short_answers_over_huge_prompts_are_a_caching_problem():
    # Same ratio as the finding on the output side, 40k tokens of prompt on the
    # input side. Downgrading the model here saves almost nothing.
    state, detail = verdict("gpt-4.1", row(requests=5000, output=95000,
                                           input_=200000000))
    assert state == "input-bound"
    assert "caching the prefix" in detail


def test_a_model_too_quiet_to_have_a_shape_gets_no_verdict():
    assert verdict("gpt-5", row(requests=40, output=760))[0] == "low-volume"
    assert verdict("gpt-5", row(requests=0, output=0))[0] == "unreadable"


def test_tiers_are_conservative_about_what_they_claim_to_know():
    assert tier("ft:gpt-4o-mini-2024-07-18:acme::AbC123") == "custom"
    assert tier("text-embedding-3-large") == "small"
    assert tier("some-model-we-have-never-heard-of") == "unknown"
    assert sibling("some-model-we-have-never-heard-of") is None
    assert verdict("ft:gpt-4o-2024-08-06:acme::X", row())[0] == "custom-model"
    assert verdict("some-model-we-have-never-heard-of", row())[0] == "unknown-model"


def test_buckets_are_folded_before_the_division():
    pages = [bucket(**{"gpt-5": (100, 50000, 1000, "proj_a")}),
             bucket(**{"gpt-5": (900, 450000, 9000, "proj_b")})]
    folded = fold(pages)
    assert folded["gpt-5"]["requests"] == 1000
    assert folded["gpt-5"]["output"] == 10000
    assert folded["gpt-5"]["projects"] == ["proj_a", "proj_b"]
    # 10000/1000 = 10 tokens a call. Averaging the two buckets' quotients would
    # have given (10 + 10) / 2 by luck here and something wrong on real data.
    assert "mean output 10 token(s)" in verdict("gpt-5", folded["gpt-5"],
                                                min_requests=100)[1]


def test_permissions_say_whether_the_expensive_model_can_come_back():
    assert permissions_state({"mode": "deny_list", "model_ids": []},
                             "gpt-5") == "unconstrained"
    assert permissions_state({"mode": "deny_list", "model_ids": ["gpt-5"]},
                             "gpt-5") == "blocked"
    assert permissions_state({"mode": "allow_list", "model_ids": ["gpt-5-mini"]},
                             "gpt-5") == "blocked"
    assert permissions_state({"mode": "allow_list", "model_ids": ["gpt-5"]},
                             "gpt-5") == "allowed"
    assert permissions_state({}, "gpt-5") == "unreadable"
    assert permissions_state(None, "gpt-5") == "unreadable"


def test_spend_is_matched_to_the_model_and_not_to_its_siblings():
    # The repair line quotes a dollar figure, so a substring match that swept in
    # the mini model would overstate exactly the number a reader acts on.
    spend = {"gpt-5, input tokens": 3000.00,
             "gpt-5, output tokens": 411.20,
             "gpt-5-mini, input tokens": 90.00,
             "ft:gpt-5:acme::x, input tokens": 12.00}
    assert spend_for("gpt-5", spend) == 3411.20
    assert spend_for("gpt-5-mini", spend) == 90.00
    assert spend_for("", spend) == 0.0
    assert spend_for("gpt-5", {}) == 0.0
