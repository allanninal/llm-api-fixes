from openai_owner_ratio_audit import (admin_key_owners, humans, mask,
                                        owner_ratio, project_owner_share,
                                        repair_lines, role_counts, role_of,
                                        unused_privilege, verdict)

NOW = 1_780_000_000


def user(uid, email, role="reader", service=False, scim=False, last_used=NOW,
         added=1_700_000_000):
    return {"id": uid, "email": email, "role": role,
            "is_service_account": service, "is_scim_managed": scim,
            "api_key_last_used_at": last_used, "added_at": added}


ROSTER = [
    user("u_1", "ada@example.com", "owner", last_used=None),
    user("u_2", "mel@example.com", "owner", last_used=NOW - 214 * 86400),
    user("u_3", "pat@example.com", "owner"),
    user("u_4", "sam@example.com", "owner", scim=True),
    user("u_5", "kim@example.com", "owner", scim=True),
    user("u_6", "rob@example.com", "reader"),
    user("u_7", "jo@example.com", "reader"),
    user("sa_1", "ingest@svc", "owner", service=True),
    user("sa_2", "batch@svc", "owner", service=True),
    user("sa_3", "evals@svc", "owner", service=True),
]


def test_service_accounts_never_count_toward_the_owner_ratio():
    # The trap in one assertion. The raw list is 8 owners of 10; the roster of
    # people is 5 of 7. Only the second is a statement about access control,
    # and the first recommends demoting a cron job.
    assert round(owner_ratio(role_counts(ROSTER)), 2) == 0.80
    people = humans(ROSTER)
    assert len(people) == 7
    counts = role_counts(people)
    assert counts == {"owner": 5, "reader": 2, "other": 0}
    state, detail = verdict(counts)
    assert state == "owner-majority"
    assert "5 of 7" in detail


def test_a_small_organization_is_never_graded():
    two = [user("u_1", "a@x.com", "owner"), user("u_2", "b@x.com", "owner")]
    state, detail = verdict(role_counts(humans(two)))
    assert state == "too-few-members"
    assert "too few" in detail
    assert repair_lines(state) == []


def test_everyone_being_an_owner_is_its_own_state():
    roster = [user("u_%d" % i, "p%d@x.com" % i, "owner") for i in range(6)]
    state, detail = verdict(role_counts(humans(roster)))
    assert state == "everyone-is-owner"
    assert "stopped existing" in detail


def test_a_high_count_at_a_low_share_says_the_ceiling_is_a_convention():
    roster = ([user("o_%d" % i, "o%d@x.com" % i, "owner") for i in range(6)]
              + [user("r_%d" % i, "r%d@x.com" % i, "reader") for i in range(34)])
    state, detail = verdict(role_counts(humans(roster)))
    assert state == "owner-count-high"
    assert "convention rather than a platform rule" in detail


def test_no_recorded_key_use_is_a_question_and_not_a_verdict():
    quiet, note = unused_privilege(ROSTER[0], NOW)
    assert quiet is True and note == "no API key use on record"
    old, note = unused_privilege(ROSTER[1], NOW)
    assert old is True and "214 day(s) ago" in note
    fresh, note = unused_privilege(ROSTER[2], NOW)
    assert fresh is False
    assert unused_privilege({"api_key_last_used_at": "yesterday"}, NOW)[0] is True


def test_scim_managed_owners_get_a_repair_pointed_somewhere_else():
    owners = [p for p in humans(ROSTER) if role_of(p) == "owner"]
    scim = [p for p in owners if p["is_scim_managed"]]
    assert len(scim) == 2
    lines = repair_lines("owner-majority", len(scim), 1, 3)
    assert any("identity provider" in line and "reverted at the next sync" in line
               for line in lines)
    assert any("Revoke the key before" in line for line in lines)
    assert any("org-level demotion alone" in line for line in lines)


def test_the_admin_key_index_reads_the_owner_block_and_nothing_else():
    keys = [{"id": "key_admin_1", "name": "ci",
             "owner": {"id": "u_3", "name": "Pat", "type": "user"}},
            {"id": "key_admin_2", "owner": {"user": {"id": "u_9",
                                                     "email": "x@y.com"}}},
            {"id": "key_admin_3", "owner": {}}]
    index = admin_key_owners(keys)
    assert index == {"u_3": "Pat", "u_9": "x@y.com"}
    assert admin_key_owners(None) == {}


def test_project_roles_are_the_second_level():
    members = [{"id": "u_1", "role": "owner"}, {"id": "u_2", "role": "owner"},
               {"id": "u_3", "role": "owner"},
               {"id": "sa_1", "role": "owner", "is_service_account": True}]
    assert project_owner_share(members) == (3, 3, 1.0)
    mixed = [{"id": "u_1", "role": "owner"}, {"id": "u_2", "role": "member"},
             {"id": "u_3", "role": "member"}]
    owners, total, ratio = project_owner_share(mixed)
    assert (owners, total) == (1, 3) and round(ratio, 2) == 0.33
    assert project_owner_share([]) == (0, 0, 0.0)


def test_unknown_roles_are_never_counted_as_restricted():
    assert role_of({"role": "OWNER"}) == "owner"
    assert role_of({"role": "reader"}) == "reader"
    assert role_of({"role": "billing"}) == "other"
    assert role_of({}) == "other"
    assert owner_ratio({}) == 0.0


def test_emails_are_masked():
    assert mask("ada@example.com") == "a***@example.com"
    assert mask("service-account") == "service-account"
    assert mask(None) == "unknown"
