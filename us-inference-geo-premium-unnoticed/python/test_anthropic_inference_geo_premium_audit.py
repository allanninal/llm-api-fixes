from anthropic_inference_geo_premium_audit import (fold, geo_of,
                                                   premium_estimate,
                                                   residency_default, tokens_of,
                                                   us_share, verdict)


def result(geo="us", workspace="wrkspc_01Qy", uncached=100_000_000,
           output=8_000_000, cache_read=0, write_5m=0, write_1h=0):
    """One result from the messages usage report."""
    return {"inference_geo": geo, "workspace_id": workspace,
            "uncached_input_tokens": uncached, "output_tokens": output,
            "cache_read_input_tokens": cache_read,
            "cache_creation": {"ephemeral_5m_input_tokens": write_5m,
                               "ephemeral_1h_input_tokens": write_1h}}


def page(*results):
    return {"data": [{"starting_at": "2026-08-01T00:00:00Z",
                      "results": list(results)}], "has_more": False}


def test_the_premium_is_inside_the_billed_amount_not_added_to_it():
    # $1,100 billed at 1.1x is $1,000 of base rate and $100 of premium. The
    # tempting arithmetic, 1100 * 0.1, gives $110 and is wrong by a tenth.
    assert abs(premium_estimate(1100.0, 1.0) - 100.0) < 1e-6
    assert abs(premium_estimate(1100.0, 0.5) - 50.0) < 1e-6
    assert premium_estimate(1100.0, 0.0) == 0.0
    assert premium_estimate(0.0, 1.0) == 0.0
    # A multiplier of 1 is no premium at all, not a division by zero.
    assert premium_estimate(1100.0, 1.0, multiplier=1.0) == 0.0


def test_a_workspace_default_and_a_per_request_parameter_are_two_findings():
    totals = {"us": 400_000_000, "global": 8_000_000}
    assert verdict(totals, "us")[0] == "us-by-workspace-default"
    assert verdict(totals, "global")[0] == "us-by-request"
    assert verdict(totals, "unset")[0] == "us-unexplained"
    detail = verdict(totals, "us")[1]
    assert "98% of 408.0M priced token(s)" in detail


def test_models_that_predate_the_parameter_are_not_a_finding():
    assert verdict({"not_available": 50_000_000}, "unset")[0] == "geo-unsupported"
    assert verdict({"global": 50_000_000}, "us")[0] == "no-us-traffic"
    assert verdict({"us": 900}, "us")[0] == "low-volume"


def test_a_null_geo_is_unspecified_and_never_global():
    assert geo_of({"inference_geo": None}) == "unspecified"
    assert geo_of({}) == "unspecified"
    assert geo_of({"inference_geo": "US"}) == "us"
    assert geo_of({"inference_geo": "global"}) == "global"
    assert geo_of({"inference_geo": "not_available"}) == "not_available"


def test_every_priced_category_counts_including_the_nested_cache_writes():
    # The multiplier applies to cache writes and reads too, so a flat read that
    # misses cache_creation understates a heavily cached workspace.
    assert tokens_of(result(uncached=10, output=5, cache_read=3,
                            write_5m=2, write_1h=1)) == 21
    assert tokens_of({"uncached_input_tokens": 10, "cache_creation": None}) == 10
    assert tokens_of({}) == 0


def test_folding_keeps_workspaces_and_geos_apart():
    folded = fold([page(result(geo="us", uncached=400_000_000, output=0),
                        result(geo="global", uncached=8_000_000, output=0),
                        result(geo="us", workspace="wrkspc_02Zz",
                               uncached=1_000_000, output=0))])
    assert folded["wrkspc_01Qy"] == {"us": 400_000_000, "global": 8_000_000}
    assert folded["wrkspc_02Zz"] == {"us": 1_000_000}
    assert abs(us_share(folded["wrkspc_01Qy"]) - 400 / 408) < 1e-9
    assert us_share({}) == 0.0


def test_residency_is_read_from_the_nested_block():
    assert residency_default({"data_residency":
                              {"default_inference_geo": "us"}}) == "us"
    assert residency_default({"data_residency":
                              {"default_inference_geo": "global"}}) == "global"
    assert residency_default({"data_residency": {}}) == "unset"
    assert residency_default({}) == "unset"
    assert residency_default(None) == "unset"
