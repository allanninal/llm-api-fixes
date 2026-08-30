from anthropic_max_tokens_cap import (effective_cap, parse_path, sync_cap,
                                       tier_spans, verdict, window_of)

SONNET = {"id": "claude-sonnet-5", "max_tokens": 128_000,
          "max_input_tokens": 1_000_000}
HAIKU = {"id": "claude-haiku-4-5-20251001", "max_tokens": 64_000,
         "max_input_tokens": 200_000}


def test_the_same_value_is_legal_on_one_model_and_a_400_on_the_other():
    # The whole note. One shared constant, two tiers, one of them rejected on
    # every call from the first one.
    assert verdict(128_000, effective_cap(SONNET)[0])[0] == "at-cap"
    state, detail = verdict(128_000, effective_cap(HAIKU)[0])
    assert state == "above-cap"
    assert "against a cap of 64000" in detail
    assert "64000 over" in detail
    assert "400" in detail


def test_the_batch_ceiling_needs_the_endpoint_and_the_header_and_the_window():
    # Three inputs, and dropping any one of them gives the wrong ceiling.
    cap, source = effective_cap(SONNET, "batches", ["output-300k-2026-03-24"])
    assert (cap, "output-300k-2026-03-24" in source) == (300_000, True)
    # Same model, same header, synchronous endpoint: the model object wins.
    assert effective_cap(SONNET, "messages", ["output-300k-2026-03-24"])[0] == 128_000
    # Same model, batch endpoint, header not sent: the model object again.
    assert effective_cap(SONNET, "batches", [])[0] == 128_000
    # Header sent on a 200k-context model: it does not qualify.
    cap, source = effective_cap(HAIKU, "batches", ["output-300k-2026-03-24"])
    assert cap == 64_000
    assert "1M context model" in source


def test_a_model_object_with_no_cap_is_not_an_unlimited_one():
    assert sync_cap({"id": "claude-sonnet-5"}) is None
    assert sync_cap({"max_tokens": 0}) is None
    assert sync_cap({"max_tokens": "128000"}) is None
    assert sync_cap(None) is None
    assert window_of(HAIKU) == 200_000
    assert window_of({}) is None
    state, detail = verdict(128_000, effective_cap({"id": "x"})[0])
    assert state == "cap-unknown"
    assert "no ceiling could be read" in detail


def test_the_floor_is_one_and_it_is_a_different_finding():
    assert verdict(0, 128_000)[0] == "below-minimum"
    assert verdict(-1, 128_000)[0] == "below-minimum"
    assert verdict(1, 128_000)[0] == "within-cap"


def test_a_value_sitting_exactly_on_the_ceiling_is_its_own_warning():
    state, detail = verdict(64_000, 64_000)
    assert state == "at-cap"
    assert "any move to a smaller model breaks this path" in detail
    assert verdict(16_000, 64_000) == (
        "within-cap", "max_tokens is 16000 of a 64000 cap (25%)")


def test_one_number_shared_across_two_tiers_is_reported_before_it_breaks():
    rows = [("reports", "claude-opus-5", 64_000, 128_000),
            ("classifier", "claude-haiku-4-5-20251001", 64_000, 64_000),
            ("summaries", "claude-sonnet-5", 8_000, 128_000)]
    # 64000 passes on both today, and it is still the number the next model
    # swap turns into a 400, so it is named.
    assert tier_spans(rows) == [(64_000, ["claude-haiku-4-5-20251001",
                                          "claude-opus-5"])]
    # A value used by one model only is not a span.
    assert tier_spans(rows[2:]) == []
    assert tier_spans([]) == []
    assert tier_spans(None) == []


def test_the_shorthand_argument_parses_model_ids_that_contain_no_colon():
    assert parse_path("classifier=claude-haiku-4-5-20251001:64000") == (
        "classifier", {"model": "claude-haiku-4-5-20251001",
                       "max_tokens": 64000, "endpoint": "messages"})
    assert parse_path("reports=claude-opus-5:128000")[1]["max_tokens"] == 128000
    assert parse_path("no-colon=claude-opus-5") is None
    assert parse_path("claude-opus-5:128000") is None
    assert parse_path("reports=claude-opus-5:lots") is None
    assert parse_path("") is None
    assert parse_path(None) is None
