from openai_tenant_attribution_audit import (classify, fold, unresolved,
                                             verdict)

DIRECTORY = {
    "user_eng1": {"name": "an engineer", "service_account": False},
    "user_eng2": {"name": "another engineer", "service_account": False},
    "sa_prod": {"name": "prod-backend", "service_account": True},
}


def folded(users=None, keys=None, projects=None, requests=100000):
    return {"users": users if users is not None else {"sa_prod": 100000},
            "keys": keys if keys is not None else {"key_abc": 100000},
            "projects": projects if projects is not None else {"proj_1": 100000},
            "requests": requests}


def bucket(rows):
    """One daily bucket from the usage endpoint, grouped three ways."""
    return {"data": [{"start_time": 0, "results": [
        {"user_id": u, "api_key_id": k, "project_id": p,
         "num_model_requests": n} for (u, k, p, n) in rows]}]}


def test_every_principal_is_one_of_your_own_and_that_is_the_finding():
    # Eleven rows, none of them a customer. The API answered; the answer is
    # about the org's own service accounts.
    state, detail = verdict(
        folded(users={"sa_prod": 90000, "user_eng1": 10000},
               keys={"key_a": 60000, "key_b": 40000}),
        DIRECTORY, tenant_count=412)
    assert state == "keys-below-tenants"
    assert "2 distinct api_key_id value(s) against 412 tenant(s)" in detail
    assert "org members or service accounts rather than customers" in detail


def test_one_key_is_its_own_worst_case():
    state, detail = verdict(folded(), DIRECTORY, tenant_count=412)
    assert state == "single-key"
    assert "one bucket" in detail


def test_enough_keys_means_the_platform_can_slice():
    state, _ = verdict(
        folded(keys={"key_%d" % i: 10 for i in range(500)}),
        DIRECTORY, tenant_count=412)
    assert state == "segmented"


def test_without_a_tenant_count_the_script_does_not_invent_a_verdict():
    state, detail = verdict(folded(keys={"key_a": 5, "key_b": 5}), DIRECTORY)
    assert state == "unknown-tenant-count"
    assert "Pass the tenant count" in detail
    assert verdict({"users": {}, "keys": {}, "projects": {}, "requests": 0},
                   DIRECTORY)[0] == "no-usage"


def test_a_principal_the_directory_does_not_know_is_a_different_problem():
    f = folded(users={"user_departed": 5000, "sa_prod": 5000},
               keys={"key_a": 5000, "key_b": 5000})
    assert classify("user_departed", DIRECTORY) == "unresolved"
    assert classify("sa_prod", DIRECTORY) == "service-account"
    assert classify("user_eng1", DIRECTORY) == "member"
    assert unresolved(f, DIRECTORY) == ["user_departed"]
    assert "resolve to nobody" in verdict(f, DIRECTORY, tenant_count=412)[1]


def test_fold_counts_the_three_dimensions_and_skips_the_nulls():
    pages = [bucket([("sa_prod", "key_a", "proj_1", 700),
                     (None, "key_b", "proj_1", 300)])]
    f = fold(pages)
    assert f["requests"] == 1000
    assert f["users"] == {"sa_prod": 700}
    assert f["keys"] == {"key_a": 700, "key_b": 300}
    assert f["projects"] == {"proj_1": 1000}
