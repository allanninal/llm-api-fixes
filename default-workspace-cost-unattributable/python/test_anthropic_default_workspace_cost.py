from anthropic_default_workspace_cost import (amount, cost_by_workspace,
                                                fold_keys, key_attribution,
                                                playground_share, repair_lines,
                                                unattributed_share, usage_split,
                                                verdict, weigh)


def cost(workspace_id, value):
    return {"workspace_id": workspace_id, "amount": value, "currency": "USD"}


def use(api_key_id, workspace_id, tokens):
    return {"api_key_id": api_key_id, "workspace_id": workspace_id,
            "uncached_input_tokens": tokens}


def page(results):
    return {"data": [{"results": list(results)}], "has_more": False}


def key(kid, name, scope_type="workspace", scope_ws=None, top_ws=None,
        status="active"):
    return {"id": kid, "name": name, "status": status,
            "scope": {"type": scope_type, "workspace_id": scope_ws},
            "workspace_id": top_ws}


KEYS = [
    key("apikey_01aa", "nightly-summaries", scope_type="organization"),
    key("apikey_01bb", "ingest-worker"),
    key("apikey_01cc", "eval-runner"),
    key("apikey_01dd", "adam-scratch"),
    key("apikey_01ee", "billing-team", scope_ws="wrkspc_01"),
]


def test_the_unallocated_bucket_is_two_causes_and_one_of_them_moves():
    # The note in one assertion. A large null share, mostly from keys, with
    # four named keys to move and a playground remainder that will not budge.
    costs = cost_by_workspace([page([cost(None, "15706.09"),
                                     cost("wrkspc_01", "17000.00"),
                                     cost("wrkspc_02", "8502.46")])])
    total = round(sum(costs.values()), 2)
    share = unattributed_share(costs)
    assert round(share, 2) == 0.38
    split = usage_split([page([use(None, None, 900_000),
                               use("apikey_01bb", None, 9_100_000),
                               use("apikey_01ee", "wrkspc_01", 40_000_000)])])
    folded = fold_keys(KEYS)
    state, detail = verdict(share, total, folded, split)
    assert state == "movable-keys"
    assert "4 active key(s)" in detail
    repairs = repair_lines(state, folded, split)
    assert any("organization scope" in line for line in repairs)
    assert any("Console playground" in line for line in repairs)
    assert any("rate-limit override" in line for line in repairs)


def test_playground_traffic_has_no_key_to_move():
    # Identical cost shape, inverted usage split. Nothing about the keys
    # changed and the correct answer did: this bucket has no migration in it.
    costs = cost_by_workspace([page([cost(None, "15706.09"),
                                     cost("wrkspc_01", "25502.46")])])
    split = usage_split([page([use(None, None, 9_000_000),
                               use("apikey_01bb", None, 1_000_000)])])
    assert round(playground_share(split), 2) == 0.90
    state, detail = verdict(unattributed_share(costs),
                            round(sum(costs.values()), 2), fold_keys(KEYS), split)
    assert state == "console-playground"
    assert "no key can be moved" in detail
    assert not any("recreate each key" in line
                   for line in repair_lines(state, fold_keys(KEYS), split))


def test_the_scope_resolver_prefers_scope_over_the_deprecated_field():
    assert key_attribution(key("k", "n", scope_type="organization")) == \
        ("organization-scoped", None)
    assert key_attribution(key("k", "n", scope_ws="wrkspc_01")) == \
        ("named-workspace", "wrkspc_01")
    # Deprecated top-level field is the fallback, never the first read.
    assert key_attribution(key("k", "n", top_ws="wrkspc_09")) == \
        ("named-workspace", "wrkspc_09")
    assert key_attribution(key("k", "n", scope_ws="wrkspc_01",
                               top_ws="wrkspc_09"))[1] == "wrkspc_01"
    # No workspace anywhere: the default workspace, which has no id to report.
    assert key_attribution(key("k", "n")) == ("default-workspace", None)
    assert key_attribution({}) == ("default-workspace", None)
    # An unrecognised scope is never assumed harmless.
    assert key_attribution(key("k", "n", scope_type="service_account"))[0] == \
        "unknown-scope"


def test_a_playground_request_in_the_default_workspace_is_counted_once():
    # Both fields null. Counting it in both buckets inflates the movable half,
    # which is the half the script is about to recommend work on.
    split = usage_split([page([use(None, None, 1_000)])])
    assert split["console-playground"] == 1_000
    assert split["default-workspace"] == 0
    assert playground_share(split) == 1.0
    assert playground_share({}) == 0.0


def test_amount_is_a_decimal_string_and_null_gets_a_sentinel():
    assert amount({"amount": "1174.40"}) == 1174.40
    assert amount({"amount": None}) == 0.0
    assert amount({"amount": "not money"}) == 0.0
    assert amount(None) == 0.0
    rows = cost_by_workspace([page([cost(None, "10.00"), cost(None, "5.00"),
                                    cost("wrkspc_01", "85.00")])])
    assert rows["(default workspace)"] == 15.0
    assert round(unattributed_share(rows), 2) == 0.15
    assert unattributed_share({}) == 0.0


def test_inactive_keys_never_reach_the_migration_list():
    folded = fold_keys(KEYS + [key("apikey_01ff", "retired",
                                   status="inactive"),
                               key("apikey_01gg", "gone", status="archived")])
    ids = [k["id"] for k in folded["default-workspace"]]
    assert "apikey_01ff" not in ids and "apikey_01gg" not in ids
    assert len(folded["default-workspace"]) == 3
    assert len(folded["organization-scoped"]) == 1
    assert len(folded["named-workspace"]) == 1
    assert fold_keys(None)["default-workspace"] == []


def test_a_small_share_and_an_empty_window_are_never_findings():
    costs = cost_by_workspace([page([cost(None, "40.00"),
                                     cost("wrkspc_01", "960.00")])])
    state, _ = verdict(unattributed_share(costs), 1000.0, fold_keys(KEYS),
                       usage_split([]))
    assert state == "attributed"
    assert verdict(1.0, 0.0, fold_keys([]), {})[0] == "no-spend-yet"
    assert repair_lines("attributed", {}, {}) == []
    assert weigh({"uncached_input_tokens": 10, "output_tokens": 5,
                  "cache_creation": {"ephemeral_5m_input_tokens": 7}}) == 22
    assert weigh({"cache_creation": 3}) == 0
    assert weigh(None) == 0


def test_every_active_key_is_placed_and_the_bucket_still_has_spend():
    folded = fold_keys([key("apikey_01ee", "billing-team", scope_ws="wrkspc_01")])
    state, detail = verdict(0.31, 41_208.55, folded,
                            usage_split([page([use("apikey_01ee", None, 5)])]))
    assert state == "unattributable-no-key-to-move"
    assert "since been deleted" in detail
    assert any("do not open a migration ticket" in line
               for line in repair_lines(state, folded, {}))
