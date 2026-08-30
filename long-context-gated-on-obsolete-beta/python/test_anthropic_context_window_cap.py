from anthropic_context_window_cap import (audit, grade_betas, grade_ceiling,
                                           grade_premium, parse_rules,
                                           repair_lines, reported_output,
                                           reported_window, shortfall,
                                           valid_model_id)

OPUS_5 = {"id": "claude-opus-5", "max_input_tokens": 1_000_000,
          "max_output_tokens": 128_000}
SONNET_4_5 = {"id": "claude-sonnet-4-5", "max_input_tokens": 200_000,
              "max_output_tokens": 64_000}
HAIKU = {"id": "claude-haiku-4-5-20251001", "max_input_tokens": 200_000,
         "max_output_tokens": 64_000}


def test_a_million_token_window_enforced_at_two_hundred_thousand():
    # The note in one assertion. Nothing is misconfigured on the provider side
    # and nothing errors; the window is simply unreachable because a constant
    # in the application says so.
    rules = parse_rules({"claude-opus-5": {"max_input_tokens": 200_000}})
    state, detail = grade_ceiling(reported_window(OPUS_5),
                                  rules["claude-opus-5"]["cap"])
    assert state == "capped-in-code"
    assert "800000 token(s) of window bought and unreachable" in detail
    assert shortfall(1_000_000, 200_000) == 800_000
    assert any("raise the enforced ceiling" in line
               for line in repair_lines(state, "claude-opus-5"))


def test_the_opposite_direction_is_a_different_and_louder_fault():
    state, detail = grade_ceiling(reported_window(SONNET_4_5), 1_000_000)
    assert state == "cap-above-model"
    assert "400 prompt is too long" in detail
    # And an aligned pair is not a finding at all.
    assert grade_ceiling(reported_window(HAIKU), 200_000)[0] == "aligned"


def test_the_same_beta_header_is_two_findings_depending_on_the_model():
    inert = grade_betas(reported_window(OPUS_5), ["context-1m-2025-08-07"])
    assert [s for s, _ in inert] == ["inert-beta-header"]
    assert "does nothing" in inert[0][1]

    retired = grade_betas(reported_window(SONNET_4_5), ["context-1m-2025-08-07"])
    assert [s for s, _ in retired] == ["retired-beta"]
    assert "2026-04-30" in retired[0][1]
    # An unrelated beta is not this note's business.
    assert grade_betas(1_000_000, ["some-other-beta"]) == []
    assert grade_betas(None, ["context-1m-2025-08-07"]) == []


def test_a_long_context_premium_branch_prices_something_that_is_free():
    state, detail = grade_premium(reported_window(OPUS_5), True)
    assert state == "phantom-premium"
    assert "same per-token rate" in detail
    assert grade_premium(reported_window(OPUS_5), False) is None
    # On a 200k model there is no long-context branch to be wrong about.
    assert grade_premium(reported_window(SONNET_4_5), True) is None


def test_one_stale_id_carries_several_findings_at_once():
    rules = parse_rules({"claude-opus-5": {
        "max_input_tokens": 200_000,
        "beta_headers": "context-1m-2025-08-07",
        "long_context_premium": True}})
    states = [s for s, _ in audit(OPUS_5, rules["claude-opus-5"])]
    assert states == ["capped-in-code", "inert-beta-header", "phantom-premium"]
    # A clean model produces one quiet line and no findings.
    clean = parse_rules({"claude-haiku-4-5-20251001": {"max_input_tokens": 200_000}})
    assert [s for s, _ in audit(HAIKU, clean["claude-haiku-4-5-20251001"])] == \
        ["aligned"]


def test_model_ids_are_validated_before_they_reach_a_url():
    assert valid_model_id("claude-opus-5") is True
    assert valid_model_id("claude-haiku-4-5-20251001") is True
    assert valid_model_id("../../organizations") is False
    assert valid_model_id("claude opus 5") is False
    assert valid_model_id("") is False
    assert valid_model_id(None) is False
    rules = parse_rules({"../../etc": {"max_input_tokens": 1},
                         "claude-opus-5": {"max_input_tokens": 200_000}})
    assert list(rules) == ["claude-opus-5"]


def test_a_missing_window_is_not_a_window_of_zero():
    assert reported_window({}) is None
    assert reported_window({"max_input_tokens": 0}) is None
    assert reported_window({"max_input_tokens": "1000000"}) == 1_000_000
    assert reported_output(OPUS_5) == 128_000
    assert shortfall(None, 200_000) is None
    state, detail = grade_ceiling(None, 200_000)
    assert state == "window-not-reported"
    assert "no claim is made" in detail


def test_rules_default_safely_when_the_config_is_thin():
    rules = parse_rules({"claude-opus-5": {}})
    assert rules["claude-opus-5"] == {"cap": None, "betas": [], "premium": False}
    assert audit(OPUS_5, rules["claude-opus-5"]) == []
    assert parse_rules(None) == {}
    assert parse_rules({"claude-opus-5": "not a dict"})["claude-opus-5"]["cap"] is None
