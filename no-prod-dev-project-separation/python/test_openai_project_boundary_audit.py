from openai_project_boundary_audit import (active, environments, mixed,
                                             repair_lines, shares,
                                             spend_by_project, verdict,
                                             window_start)


def project(pid, name, status="active", archived_at=None):
    return {"id": pid, "name": name, "status": status,
            "archived_at": archived_at}


def cost(pid, value):
    return {"project_id": pid, "amount": {"value": value, "currency": "usd"}}


def buckets(*results):
    return [{"results": list(results)}]


def test_one_active_project_is_the_finding_whatever_the_bill_says():
    # The note in one assertion. There is nothing wrong with the spend; there
    # is nowhere to put a limit, an alert or a model permission.
    live = active([project("proj_a", "Default project"),
                   project("proj_old", "Prototype", status="archived")])
    assert len(live) == 1
    ranked = shares(spend_by_project(buckets(cost("proj_a", 18406.11))))
    state, detail = verdict(len(live), ranked)
    assert state == "no-boundary"
    assert "no second container" in detail
    repairs = repair_lines(state, {"prod", "ci", "local"})
    assert any("archived but never deleted" in line for line in repairs)
    assert any("key names" in line for line in repairs)


def test_a_dominant_project_in_a_split_org_is_the_other_note():
    # Identical arithmetic, opposite conclusion. This organization already has
    # the boundary this note is about, so the finding belongs to the
    # concentration reading and the script says so rather than claiming it.
    ranked = shares(spend_by_project(buckets(
        cost("proj_prod", 96_000.0), cost("proj_stage", 2_400.0),
        cost("proj_dev", 1_100.0), cost("proj_ci", 500.0))))
    state, detail = verdict(4, ranked)
    assert state == "concentration-not-topology"
    assert "different repair" in detail
    assert any("Rank the cost rows" in line for line in repair_lines(state))


def test_projects_that_exist_and_never_receive_traffic():
    ranked = shares(spend_by_project(buckets(
        cost("proj_prod", 9_900.0), cost("proj_stage", 0.0),
        cost("proj_dev", 0.0))))
    state, detail = verdict(3, ranked)
    assert state == "boundary-unused"
    assert "no traffic routes to them" in detail
    assert any("Nothing routes to them" in line for line in repair_lines(state))


def test_archived_projects_are_dropped_on_either_signal():
    rows = [project("a", "live"),
            project("b", "by status", status="archived"),
            project("c", "by timestamp", archived_at=1_700_000_000),
            project("d", "shouty", status="ARCHIVED")]
    assert [p["id"] for p in active(rows)] == ["a"]
    assert active([]) == [] and active(None) == []


def test_an_ungrouped_row_is_never_ranked_as_a_project():
    # The failure this guards: forget group_by, every project_id comes back
    # null, and the fold reports one enormous project in an org that has three.
    spend = spend_by_project(buckets(cost(None, 41_000.0), cost("proj_a", 900.0),
                                     cost("proj_b", 100.0)))
    assert spend["ungrouped"] == 41_000.0
    ranked = shares(spend)
    assert [row[0] for row in ranked] == ["proj_a", "proj_b"]
    assert round(ranked[0][2], 2) == 0.90
    assert verdict(3, ranked)[0] == "separated"


def test_environment_words_match_whole_tokens_only():
    assert environments("prod-worker") == {"prod"}
    assert environments("Local Adam") == {"local"}
    assert environments("ci-fixtures") == {"ci"}
    # The ones a substring test destroys.
    assert environments("devops-runner") == set()
    assert environments("provider-proxy") == set()
    assert environments("protest") == set()
    assert environments(None) == set()
    assert mixed(["prod-worker", "local-adam", "ci-fixtures"]) == \
        {"prod", "local", "ci"}
    assert mixed([]) == set()


def test_no_spend_and_no_projects_are_never_verdicts():
    assert verdict(0, [])[0] == "no-active-projects"
    state, detail = verdict(3, shares(spend_by_project(buckets(cost("a", 0.2)))))
    assert state == "no-spend-yet"
    assert "nothing has tested it" in detail
    assert repair_lines("separated") == []
    assert spend_by_project(None) == {} and shares(None) == []


def test_the_window_starts_at_midnight_utc():
    import datetime as dt
    now = dt.datetime(2026, 8, 31, 17, 45, 12, tzinfo=dt.timezone.utc)
    assert window_start(30, now) == int(
        dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc).timestamp())
