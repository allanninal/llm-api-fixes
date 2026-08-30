from openai_stale_invite_audit import (classify, mask, member_emails,
                                        owner_grant, project_roles,
                                        repair_lines, sent_at)

NOW = 1_780_000_000
DAY = 86400

ROSTER = member_emails([{"email": "Mel@example.com"}, {"email": "pat@example.com"}])


def invite(iid, email, role="reader", status="pending", sent_days=137,
           expires_days=107, projects=None):
    return {"id": iid, "email": email, "role": role, "status": status,
            "invited_at": NOW - sent_days * DAY,
            "expires_at": NOW - expires_days * DAY if expires_days else None,
            "projects": projects or []}


def test_a_pending_invite_past_its_expiry_is_the_row_a_status_filter_misses():
    # The note in one assertion, and then the same record relabelled. Same
    # dates, same grants, two states, two repairs.
    row = invite("invite_01hd", "rob@example.com", role="owner")
    state, detail = classify(row, ROSTER, NOW)
    assert state == "expired-but-still-pending"
    assert "filter on status alone" in detail
    assert "107 day(s) ago" in detail

    relabelled = dict(row, status="expired")
    other, detail = classify(relabelled, ROSTER, NOW)
    assert other == "expired-uncollected"
    assert "never cleaned up" in detail
    assert repair_lines(state, row) != repair_lines(other, relabelled)


def test_an_invite_for_somebody_already_on_the_roster_is_not_an_onboarding_failure():
    row = invite("invite_01me", "mel@EXAMPLE.com", sent_days=61, expires_days=31)
    state, detail = classify(row, ROSTER, NOW)
    assert state == "already-a-member"
    assert "already on the roster" in detail
    lines = repair_lines(state, row)
    assert any("no onboarding problem here" in line for line in lines)
    assert not any("re-send" in line for line in lines)


def test_an_owner_grant_hides_inside_the_project_entries():
    plain = invite("invite_01a", "jo@example.com", role="reader",
                   projects=[{"id": "proj_web", "role": "member"}])
    hidden = invite("invite_01b", "kim@example.com", role="reader",
                    projects=[{"id": "proj_ingest", "role": "owner"},
                              {"id": "proj_web", "role": "member"}])
    assert owner_grant(plain) is False
    assert owner_grant(hidden) is True
    assert owner_grant(invite("invite_01c", "x@y.com", role="owner")) is True
    assert project_roles(hidden) == [("proj_ingest", "owner"),
                                     ("proj_web", "member")]
    assert project_roles({}) == []
    lines = repair_lines("expired-but-still-pending", hidden)
    assert any("offers owner rights" in line for line in lines)
    assert any("proj_ingest=owner" in line for line in lines)


def test_a_stale_but_live_invite_is_its_own_state():
    row = invite("invite_01j", "jay@example.com", sent_days=29,
                 expires_days=-1)
    row["expires_at"] = NOW + 3 * DAY
    state, detail = classify(row, ROSTER, NOW)
    assert state == "pending-stale"
    assert "29 day(s)" in detail
    assert any("delivery status" in line for line in repair_lines(state, row))


def test_a_fresh_invite_and_an_accepted_one_are_not_findings():
    fresh = invite("invite_01f", "new@example.com", sent_days=2)
    fresh["expires_at"] = NOW + 5 * DAY
    assert classify(fresh, ROSTER, NOW)[0] == "pending"
    assert repair_lines("pending", fresh) == []
    done = invite("invite_01g", "old@example.com", status="accepted")
    assert classify(done, ROSTER, NOW)[0] == "accepted"
    assert classify({"status": "revoked", "email": "z@x.com"},
                    ROSTER, NOW)[0] == "unknown-status"


def test_the_sent_timestamp_is_read_under_either_field_name():
    assert sent_at({"invited_at": 1_700_000_000}) == 1_700_000_000
    assert sent_at({"created_at": 1_700_000_001}) == 1_700_000_001
    assert sent_at({"invited_at": None, "created_at": 1_700_000_002}) == 1_700_000_002
    assert sent_at({"invited_at": "not a date"}) is None
    assert sent_at({}) is None
    assert sent_at(None) is None
    # A missing timestamp must not make the invite look brand new.
    row = {"id": "i", "email": "q@x.com", "role": "reader", "status": "pending",
           "expires_at": NOW - DAY}
    assert classify(row, ROSTER, NOW)[0] == "expired-but-still-pending"


def test_every_repair_ends_with_the_delete_and_masks_the_address():
    row = invite("invite_01hd", "rob@example.com", role="owner")
    assert repair_lines("expired-but-still-pending", row)[-1] == \
        "DELETE /v1/organization/invites/invite_01hd"
    assert mask("rob@example.com") == "r***@example.com"
    assert mask(None) == "unknown"
    assert member_emails(None) == set()
