import datetime as dt

from openai_key_rotation_clock import (age_days, corroboration,
                                       group_by_account, newest_and_oldest,
                                       rotation_plan, rotation_verdict,
                                       service_account_id)

NOW = dt.datetime(2026, 8, 31, 12, 0, 0, tzinfo=dt.timezone.utc)
ACCOUNT = {"id": "svc_1", "name": "ingest-worker", "created_at": 0}


def unix(days_ago):
    return int((NOW - dt.timedelta(days=days_ago)).timestamp())


def sa_key(key_id, days_ago, account="svc_1"):
    return {"id": key_id, "created_at": unix(days_ago),
            "owner": {"type": "service_account",
                      "service_account": {"id": account, "name": "ingest-worker"}}}


def test_the_newest_key_is_the_clock_and_the_oldest_would_lie():
    # An account rotated last week still holds the retired key until somebody
    # revokes it. A reader that takes the oldest created_at calls that account
    # two years stale, which is the single mistake that would make this note
    # useless. Reading the newest calls it a rotation that has not been
    # finished, which is what it is.
    rotated = [sa_key("key_new", 45), sa_key("key_old", 731)]
    newest, oldest = newest_and_oldest(rotated, NOW)
    assert (newest, oldest) == (45, 731)
    state, detail = rotation_verdict(ACCOUNT, rotated, NOW)
    assert state != "single-stale-key" and state != "stale-key"
    assert state == "unfinished-rotation"
    assert "newest key 45 day(s) old" in detail

    # And once the retired key is revoked, there is nothing left to report.
    finished = rotation_verdict(ACCOUNT, [sa_key("key_new", 45)], NOW)
    assert finished[0] == "rotating"
    assert "45 day(s) old" in finished[1]


def test_the_key_count_produces_three_different_findings():
    single = rotation_verdict(ACCOUNT, [sa_key("key_a", 731)], NOW)
    assert single[0] == "single-stale-key"
    assert "it is the only one" in single[1]
    assert any("mint a second key first" in step
               for step in rotation_plan("proj_1", "ingest-worker", True))

    both_old = rotation_verdict(ACCOUNT, [sa_key("key_a", 402),
                                          sa_key("key_b", 500)], NOW)
    assert both_old[0] == "stale-key"
    assert "across 2 key(s)" in both_old[1]
    assert not any("mint a second key first" in step
                   for step in rotation_plan("proj_1", "ingest-worker", False))

    halfway = rotation_verdict(ACCOUNT, [sa_key("key_a", 12),
                                         sa_key("key_b", 588)], NOW)
    assert halfway[0] == "unfinished-rotation"
    assert "still live" in halfway[1]


def test_an_empty_or_unreachable_audit_log_is_never_corroboration():
    unreachable = corroboration([], "proj_1", audit_reachable=False)
    assert unreachable[0] == "audit-unavailable"
    assert "silence is not evidence" in unreachable[1]

    empty = corroboration([], "proj_1", audit_reachable=True)
    assert empty[0] == "audit-unavailable"
    assert "nothing is being recorded" in empty[1]


def test_the_audit_log_confirms_a_project_and_never_an_account():
    elsewhere = [{"type": "api_key.created", "project": {"id": "proj_other"}}]
    state, detail = corroboration(elsewhere, "proj_1", True, 180)
    assert state == "confirmed-at-project-level"
    assert "project-level fact" in detail
    assert "does not name" in detail or "not the service account" in detail

    here = [{"type": "api_key.created", "project": {"id": "proj_1"}},
            {"type": "api_key.created", "project": {"id": "proj_1"}}]
    state, detail = corroboration(here, "proj_1", True, 180)
    assert state == "creation-activity-in-window"
    assert "neither confirms nor clears" in detail


def test_a_personal_key_is_not_counted_towards_a_service_account():
    keys = [sa_key("key_a", 731),
            {"id": "key_user", "created_at": unix(2),
             "owner": {"type": "user", "user": {"email": "dev@example.test"}}},
            {"id": "key_odd", "created_at": unix(2), "owner": None}]
    grouped = group_by_account(keys)
    assert list(grouped) == ["svc_1"]
    assert len(grouped["svc_1"]) == 1
    # Counting the personal key here would turn a single-key account into a
    # two-key one and hide the fact that no overlap window has ever existed.
    assert rotation_verdict(ACCOUNT, grouped["svc_1"], NOW)[0] == "single-stale-key"
    assert service_account_id({"owner": {"type": "service_account"}}) is None
    assert service_account_id(None) is None
    assert group_by_account([]) == {}


def test_a_service_account_with_no_keys_and_one_too_new_to_judge():
    empty = rotation_verdict({"id": "svc_2", "name": "search-indexer",
                              "created_at": unix(300)}, [], NOW)
    assert empty[0] == "service-account-with-no-keys"
    assert "300 day(s) ago" in empty[1]
    fresh = rotation_verdict(ACCOUNT, [sa_key("key_a", 4)], NOW)
    assert fresh[0] == "too-new"
    unreadable = rotation_verdict(ACCOUNT, [{"id": "key_a", "created_at": None,
                                             "owner": None}], NOW)
    assert unreadable[0] == "too-new"


def test_ages_are_read_from_unix_seconds_only():
    assert age_days(unix(180), NOW) == 180
    assert age_days(None, NOW) is None
    assert age_days("", NOW) is None
    assert age_days(True, NOW) is None
    assert age_days("not a number", NOW) is None


def test_the_rotation_plan_revokes_last_and_names_the_missing_field():
    steps = rotation_plan("proj_prod", "ingest-worker", False)
    assert len(steps) == 3
    assert "returned exactly once" in steps[0]
    assert "last_used_at should stop advancing" in steps[1]
    assert steps[2].startswith("revoke the old key")
    assert "no expires_at" in steps[2]
