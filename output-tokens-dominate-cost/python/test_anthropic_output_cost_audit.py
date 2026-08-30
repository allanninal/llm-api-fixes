from anthropic_output_cost_audit import (amount, bucket_of, by_bucket,
                                          top_model, verdict)


def cost(token_type, value, description="Claude Sonnet 5"):
    # amount arrives as a decimal STRING on this endpoint, not a number.
    return {"currency": "USD", "amount": str(value), "token_type": token_type,
            "description": description, "cost_type": "tokens"}


def cost_day(*rows):
    return {"starting_at": "2026-08-01T00:00:00Z", "results": list(rows)}


def usage_day(*rows):
    return {"starting_at": "2026-08-01T00:00:00Z", "results": list(rows)}


def test_amount_is_a_string_on_this_endpoint():
    assert amount({"amount": "12.34"}) == 12.34
    assert amount({"amount": 12.34}) == 12.34
    assert amount({"amount": ""}) == 0.0
    assert amount({}) == 0.0
    assert amount({"amount": "n/a"}) == 0.0


def test_token_types_fold_into_buckets_by_shape_not_by_exact_name():
    assert bucket_of("output_tokens") == "output"
    assert bucket_of("uncached_input_tokens") == "input"
    assert bucket_of("cache_read_input_tokens") == "cache_read"
    assert bucket_of("cache_creation_input_tokens") == "cache_write"
    assert bucket_of("1h_cache_creation_input_tokens") == "cache_write"
    # A type that does not exist yet must stay visible rather than vanish.
    assert bucket_of("some_future_tier_tokens") == "other"
    assert bucket_of(None) == "other"


def test_unrecognised_types_stay_in_the_denominator():
    rows = by_bucket([cost_day(cost("output_tokens", "60"),
                               cost("some_future_tier_tokens", "40"))])
    assert rows["other"] == 40.0
    state, detail = verdict(rows)
    assert "unrecognised 40%" in detail
    assert state == "output-led"


def test_the_same_spend_split_three_ways_gives_three_different_repairs():
    output_heavy = by_bucket([cost_day(cost("output_tokens", "800"),
                                       cost("uncached_input_tokens", "200"))])
    input_heavy = by_bucket([cost_day(cost("output_tokens", "300"),
                                      cost("uncached_input_tokens", "500"),
                                      cost("cache_read_input_tokens", "200"))])
    even = by_bucket([cost_day(cost("output_tokens", "450"),
                               cost("uncached_input_tokens", "550"))])

    assert verdict(output_heavy)[0] == "output-dominated"
    assert verdict(input_heavy)[0] == "input-dominated"
    assert verdict(even)[0] == "balanced"


def test_an_output_dominated_bill_names_the_only_lever_there_is():
    rows = by_bucket([cost_day(cost("output_tokens", "800"),
                               cost("uncached_input_tokens", "200"))])
    _, detail = verdict(rows)
    assert "no caching discount" in detail
    assert "5x input" in detail


def test_cache_writes_without_reads_is_its_own_finding():
    # Writes cost more than base input; without reads to amortise them the
    # caching is a premium being paid for nothing.
    rows = by_bucket([cost_day(cost("cache_creation_input_tokens", "400"),
                               cost("cache_read_input_tokens", "50"),
                               cost("output_tokens", "300"),
                               cost("uncached_input_tokens", "250"))])
    state, detail = verdict(rows)
    assert state == "cache-write-heavy"
    assert "amortise" in detail


def test_output_between_half_and_seventy_percent_is_not_an_emergency():
    rows = by_bucket([cost_day(cost("output_tokens", "550"),
                               cost("uncached_input_tokens", "450"))])
    assert verdict(rows)[0] == "output-led"


def test_a_quiet_window_reports_nothing_rather_than_a_noisy_share():
    rows = by_bucket([cost_day(cost("output_tokens", "0.10"))])
    assert verdict(rows)[0] == "no-spend"
    assert verdict(by_bucket([]))[0] == "no-spend"


def test_top_model_names_where_an_effort_change_would_land():
    model, share = top_model([
        usage_day({"model": "claude-opus-5", "output_tokens": 900,
                   "uncached_input_tokens": 4000},
                  {"model": "claude-sonnet-5", "output_tokens": 100,
                   "uncached_input_tokens": 8000}),
    ])
    assert model == "claude-opus-5"
    assert round(share, 2) == 0.9
    assert top_model([]) == (None, 0.0)
