from anthropic_long_context_audit import (band, cached_share, fold,
                                           long_share, uncached_cost, verdict)


def result(window="200k-1M", model="claude-opus-5", uncached=400_000_000,
           cache_read=0):
    """One result from the messages usage report."""
    return {"context_window": window, "model": model,
            "uncached_input_tokens": uncached,
            "cache_read_input_tokens": cache_read}


def page(*results):
    return {"data": [{"starting_at": "2026-08-01T00:00:00Z",
                      "results": list(results)}], "has_more": False}


def rows(long_uncached=400_000_000, long_read=0, short_uncached=160_000_000,
         unbanded=0):
    """A folded model row shaped like fold() returns them."""
    out = {"200k-1M": {"uncached": long_uncached, "cache_read": long_read},
           "0-200k": {"uncached": short_uncached, "cache_read": 0}}
    if unbanded:
        out["unbanded"] = {"uncached": unbanded, "cache_read": 0}
    return out


def test_a_null_context_window_is_unbanded_and_not_the_short_band():
    # The load-bearing one. 400M long against 160M short is 71%. Counting a
    # further 400M of unbanded traffic as short would report 41% and nothing
    # would ever be looked at.
    assert band({"context_window": None}) == "unbanded"
    assert band({}) == "unbanded"
    assert band({"context_window": "200k-1M"}) == "200k-1M"
    assert band({"context_window": "0-200k"}) == "0-200k"
    with_nulls = rows(unbanded=400_000_000)
    assert abs(long_share(with_nulls) - 400 / 560) < 1e-9
    state, detail = verdict(with_nulls)
    assert state == "long-context-uncached"
    assert "71% of banded uncached input" in detail


def test_a_cached_long_prefix_is_a_different_state_with_a_different_sentence():
    state, detail = verdict(rows(long_uncached=40_000_000,
                                 long_read=360_000_000,
                                 short_uncached=10_000_000))
    assert state == "long-context-cached"
    assert "It is still just as long" in detail


def test_a_short_context_workload_is_not_a_finding():
    assert verdict(rows(long_uncached=10_000_000,
                        short_uncached=400_000_000))[0] == "short-context"
    assert verdict(rows(long_uncached=100, short_uncached=100))[0] == "low-volume"


def test_traffic_the_report_never_banded_is_reported_as_such():
    state, detail = verdict({"unbanded": {"uncached": 400_000_000, "cache_read": 0}})
    assert state == "unbanded-only"
    assert "cannot be placed in a band" in detail


def test_the_cached_share_is_read_inside_the_band():
    assert cached_share({"uncached": 0, "cache_read": 100}) == 1.0
    assert cached_share({"uncached": 100, "cache_read": 0}) == 0.0
    assert cached_share({"uncached": 50, "cache_read": 50}) == 0.5
    assert cached_share({}) == 0.0


def test_the_rate_is_supplied_rather_than_baked_in():
    # 408M uncached input tokens at $5 per million.
    assert uncached_cost(408_000_000, 5.0) == 2040.0
    assert uncached_cost(0, 5.0) == 0.0
    assert uncached_cost(1_000_000, 0.0) == 0.0


def test_folding_keeps_models_and_bands_apart():
    folded = fold([page(result(window="200k-1M", uncached=200_000_000),
                        result(window="200k-1M", uncached=200_000_000,
                               cache_read=5_000_000),
                        result(window="0-200k", uncached=160_000_000),
                        result(window=None, model="claude-haiku-4-5-20251001",
                               uncached=9_000_000))])
    assert folded["claude-opus-5"]["200k-1M"]["uncached"] == 400_000_000
    assert folded["claude-opus-5"]["200k-1M"]["cache_read"] == 5_000_000
    assert folded["claude-opus-5"]["0-200k"]["uncached"] == 160_000_000
    assert folded["claude-haiku-4-5-20251001"]["unbanded"]["uncached"] == 9_000_000
