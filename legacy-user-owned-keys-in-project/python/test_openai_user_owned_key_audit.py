from openai_user_owned_key_audit import (fold_costs, migration_plan,
                                          owner_kind, owner_label,
                                          project_note, safe_hint, spend_line,
                                          spend_of, verdict)


def user_key(key_id, name, email):
    return {"id": key_id, "name": name, "redacted_value": "sk-...9c31",
            "owner": {"type": "user", "user": {"id": "user_1", "email": email}},
            "owner_project_access": "active"}


def service_key(key_id, name):
    return {"id": key_id, "name": name, "redacted_value": "sk-...aa02",
            "owner": {"type": "service_account",
                      "service_account": {"id": "svc_1", "name": "ingest"}}}


def cost_page(rows):
    return {"data": [{"results": [
        {"api_key_id": key_id, "amount": {"value": value, "currency": currency}}
        for key_id, value, currency in rows]}], "has_more": False}


def test_the_share_of_the_bill_is_not_part_of_the_verdict():
    # The line between this note and the published concentration note, as an
    # assertion. One key holding 3% of production grades exactly as one
    # holding 95%, and an even split is two findings rather than none.
    key = user_key("key_1", "api-main", "marco@example.test")
    tiny = verdict(key, 340.00, service_account_count=2)
    huge = verdict(key, 11402.88, service_account_count=2)
    assert tiny[0] == huge[0] == "personal-key-in-production"
    assert tiny[1] == huge[1]

    even = fold_costs([cost_page([("key_1", 5000.0, "USD"),
                                  ("key_2", 5000.0, "USD")])])
    a = verdict(user_key("key_1", "api-main", "marco@example.test"),
                spend_of(even, "key_1"), 1)
    b = verdict(user_key("key_2", "worker-2", "dana@example.test"),
                spend_of(even, "key_2"), 1)
    assert [a[0], b[0]] == ["personal-key-in-production"] * 2


def test_a_personal_key_with_no_traffic_is_a_different_repair():
    key = user_key("key_9", "scratch", "marco@example.test")
    state, detail = verdict(key, 0.0, service_account_count=2)
    assert state == "personal-key-idle"
    assert "revocation rather than a migration" in detail
    assert verdict(service_key("key_s", "ingest"), 90000.0, 2)[0] == \
        "service-account-key"


def test_an_unrecognised_owner_is_never_folded_into_either_camp():
    assert owner_kind({"owner": {"type": "user"}}) == "user"
    assert owner_kind({"owner": {"type": "SERVICE_ACCOUNT"}}) == "service_account"
    assert owner_kind({"owner": {"type": "robot"}}) == "unknown"
    assert owner_kind({"owner": None}) == "unknown"
    assert owner_kind({}) == "unknown"
    assert owner_kind(None) == "unknown"
    state, detail = verdict({"owner": {"type": "robot"}}, 4000.0, 3)
    assert state == "unattributable-owner"
    assert "whose lifecycle" in detail
    assert owner_label({"owner": {"type": "robot"}}) == "(owner type 'robot')"
    assert owner_label(user_key("k", "n", "d@example.test")) == "d@example.test"
    assert owner_label(service_key("k", "n")) == "ingest"
    assert owner_label({}) == "(no owner block)"


def test_an_empty_service_account_roster_is_a_project_level_finding():
    assert project_note("proj_prod", 2, 0) == \
        "project proj_prod: no service accounts at all, and 2 user-owned key(s) are spending"
    assert project_note("proj_prod", 2, 3) is None
    assert project_note("proj_evals", 0, 0) is None
    state, detail = verdict(user_key("key_1", "api-main", "m@example.test"),
                            9000.0, service_account_count=0)
    assert state == "personal-key-in-production"
    assert "no service accounts at all" in detail


def test_two_currencies_are_reported_side_by_side_and_never_added():
    costs = fold_costs([cost_page([("key_1", 400.0, "USD"),
                                   ("key_1", 300.0, "USD"),
                                   ("key_1", 120.0, "EUR")])])
    assert costs["key_1"] == {"USD": 700.0, "EUR": 120.0}
    line = spend_line(costs, "key_1", 30)
    assert line == "120.00 EUR + 700.00 USD over 30 day(s)"
    assert "820" not in line
    # The threshold comparison uses the largest single currency, never a total.
    assert spend_of(costs, "key_1") == 700.0
    assert spend_of(costs, "key_absent") == 0.0
    assert spend_line(costs, "key_absent", 30) == "no cost rows in 30 day(s)"


def test_cost_rows_that_cannot_be_read_are_skipped_rather_than_guessed():
    costs = fold_costs([{"data": [{"results": [
        {"api_key_id": None, "amount": {"value": 5.0, "currency": "USD"}},
        {"api_key_id": "key_1", "amount": None},
        {"api_key_id": "key_1", "amount": {"value": "many", "currency": "USD"}},
        {"api_key_id": "key_1", "amount": {"value": 12.5}},
    ]}]}])
    assert costs == {"key_1": {"USD": 12.5}}
    assert fold_costs([]) == {}
    assert fold_costs(None) == {}


def test_the_migration_puts_the_revocation_last():
    steps = migration_plan("proj_prod", "key_1", "api-main")
    assert len(steps) == 4
    assert "service_accounts" in steps[0]
    assert "returned exactly once" in steps[1]
    assert "confirm the spend has moved off" in steps[2]
    assert steps[3].startswith("only then revoke")
    assert safe_hint("sk-...9c31") == "sk-...9c31"
    assert safe_hint("sk-fake-whole-value-here") == "(hint withheld)"
    assert safe_hint(None) == "(no hint)"
