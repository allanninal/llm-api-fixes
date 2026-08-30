from llm_egress_region_probe import (BLOCK_CODE, blob, classify, compare,
                                     error_code, load_baseline, observation,
                                     repair_lines)


def blocked(provider="openai"):
    return observation(provider, 403,
                       {"error": {"message": "Country, region, or territory "
                                             "not supported.",
                                  "type": "invalid_request_error",
                                  "code": BLOCK_CODE}})


def ok(provider="openai"):
    return observation(provider, 200, {"data": [], "object": "list"})


def test_the_pair_is_what_turns_a_403_into_a_statement_about_geography():
    state, detail = compare(blocked(), ok())
    assert state == "geography-isolated"
    assert "not the credential" in detail
    lines = repair_lines(state)
    assert any("regions: ['iad1']" in line for line in lines)
    assert any("Move the workload, not the packets" in line for line in lines)
    # A repair that routes around the block is never printed.
    assert not any("proxy the" in line.lower() for line in lines)


def test_blocked_from_both_hosts_is_the_account_and_not_this_deployment():
    state, detail = compare(blocked(), blocked())
    assert state == "region-blocked-everywhere"
    assert "organization-level restriction" in detail
    assert any("moving this deployment will not help" in line
               for line in repair_lines(state))


def test_a_401_from_both_hosts_is_handed_to_the_credential_question():
    unauth = observation("openai", 401, {"error": {"code": "invalid_api_key"}})
    state, detail = compare(unauth, unauth)
    assert state == "credentials-not-geography"
    assert "not the location" in detail
    assert any("not this note" in line for line in repair_lines(state))

    state, _ = compare(unauth, ok())
    assert state == "credentials-here-only"
    assert any("different value in the environment" in line
               for line in repair_lines(state))


def test_one_observation_refuses_to_conclude_even_with_the_documented_code():
    state, detail = compare(blocked(), None)
    assert state == "region-blocked-unconfirmed"
    assert "has not been separated from an account-level restriction" in detail
    assert any("host you already trust" in line for line in repair_lines(state))
    assert compare(ok(), None)[0] == "clear"


def test_the_blob_round_trips_and_carries_no_credential():
    line = blob([blocked(), ok("anthropic")])
    assert "sk-" not in line and "api" not in line.lower().replace("api.", "")
    assert line == ('{"anthropic":{"code":"","status":200},'
                    '"openai":{"code":"unsupported_country_region_territory",'
                    '"status":403}}')
    back = load_baseline(line)
    assert classify(back["openai"])[0] == "region-blocked"
    assert classify(back["anthropic"])[0] == "reachable"
    # A mangled paste produces no baseline and an instruction, not a traceback.
    assert load_baseline("{not json") == {}
    assert load_baseline(None) == {}


def test_an_undocumented_403_is_recorded_rather_than_attributed():
    other = observation("anthropic", 403,
                        {"error": {"type": "permission_error",
                                   "message": "..."}})
    state, detail = classify(other)
    assert state == "forbidden-other"
    assert "permission_error" in detail
    verdict, why = compare(other, ok("anthropic"))
    assert verdict == "forbidden-unexplained"
    assert "not one this script can attribute" in why
    assert any("supported regions list" in line for line in repair_lines(verdict))


def test_bodies_are_read_in_either_envelope_and_odd_ones_do_not_raise():
    assert error_code({"error": {"code": "a", "type": "b"}}) == "a"
    assert error_code({"error": {"type": "b"}}) == "b"
    assert error_code({"error": "a string"}) == ""
    assert error_code(None) == ""
    assert observation("openai", None, None)["status"] is None
    assert classify(observation("openai", None, None))[0] == "unreachable"
    assert classify(observation("openai", 429, None))[0] == "rate-limited"
    assert repair_lines("clear") == []
