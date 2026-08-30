from openai_vector_store_expiry_audit import (anchor_note, drift_seconds,
                                               expiry_at, expiry_state, id_set,
                                               idle_seconds, policy,
                                               repair_lines)

NOW = 1_800_000_000
DAY = 86400


def store(sid="vs_a1", name="handbook", status="completed", days=None,
          anchor="last_active_at", expires_at=None, last_active_at=None,
          usage_bytes=41_000_000):
    row = {"id": sid, "name": name, "status": status, "usage_bytes": usage_bytes,
           "last_active_at": last_active_at, "expires_at": expires_at,
           "file_counts": {"total": 9, "completed": 9, "failed": 0,
                           "in_progress": 0, "cancelled": 0}}
    if days is not None:
        row["expires_after"] = {"anchor": anchor, "days": days}
    return row


def test_an_expired_store_has_no_repair_that_touches_the_policy():
    # The one state in this note where nothing can be recovered. Saying "clear
    # the policy" here would be a change that accomplishes nothing at all.
    dead = store(status="expired", days=7, expires_at=NOW - 84 * DAY)
    state, detail = expiry_state(dead, NOW)
    assert state == "expired"
    assert "84 day(s) ago" in detail
    assert "not recoverable" in detail
    lines = repair_lines(state)
    assert any("re-ingest into a new store" in line for line in lines)
    assert not any("clear it by updating" in line for line in lines)


def test_the_same_timer_is_a_finding_only_on_a_store_you_called_permanent():
    live = store(sid="vs_a1", days=7, expires_at=NOW + 2 * DAY,
                 last_active_at=NOW - 5 * DAY)
    temp = store(sid="vs_e5", name="session-uploads", days=7,
                 expires_at=NOW + 2 * DAY, last_active_at=NOW - 5 * DAY)
    assert expiry_state(live, NOW, {"vs_a1"})[0] == "policy-on-permanent"
    assert expiry_state(temp, NOW, {"vs_a1"})[0] == "expiring-soon"
    assert any("has to be no policy at all" in line
               for line in repair_lines("policy-on-permanent"))


def test_the_reported_expiry_wins_and_the_drift_is_only_printed():
    # last_active_at + 7d would put this three hours earlier than the API says.
    # The decision uses the API's number; the gap is reported, never resolved.
    drifting = store(days=7, last_active_at=NOW - 5 * DAY,
                     expires_at=NOW + 2 * DAY + 3 * 3600)
    assert drift_seconds(drifting) == 3 * 3600
    left = (expiry_at(drifting) - NOW) / DAY
    assert 2.1 < left < 2.2
    assert expiry_state(drifting, NOW, set(), notice_days=7)[0] == "expiring-soon"
    assert drift_seconds(store(days=7)) is None
    assert drift_seconds(store(expires_at=NOW)) is None


def test_the_anchor_is_only_mentioned_when_it_is_not_the_documented_one():
    assert anchor_note(store(days=7)) is None
    assert anchor_note(store(days=7, anchor="last_active_at")) is None
    assert anchor_note(store()) is None
    note = anchor_note(store(days=7, anchor="created_at"))
    assert "created_at" in note and "last_active_at" in note


def test_a_policy_with_no_usable_day_count_reads_as_no_policy():
    assert policy(store(days=7)) == ("last_active_at", 7)
    assert policy(store()) is None
    assert policy({"expires_after": {"anchor": "last_active_at"}}) is None
    assert policy({"expires_after": {"anchor": "last_active_at", "days": 0}}) is None
    assert policy({"expires_after": "7 days"}) is None
    assert policy(None) is None


def test_a_store_with_no_policy_is_reported_as_a_bill_not_a_pass():
    state, detail = expiry_state(store(usage_bytes=43_200_512), NOW)
    assert state == "permanent"
    assert "41.2 MiB retained and billed" in detail
    assert any("billed by the hour" in line for line in repair_lines(state))


def test_the_clock_helpers_tolerate_a_missing_field():
    assert idle_seconds(store(last_active_at=NOW - 3 * DAY), NOW) == 3 * DAY
    assert idle_seconds(store(), NOW) is None
    assert expiry_at(store()) is None
    assert expiry_at({"expires_at": "soon"}) is None
    assert id_set("vs_a1, vs_b2", ["vs_a1"]) == {"vs_a1", "vs_b2"}
    assert id_set(None) == set()
    far = store(days=90, expires_at=NOW + 60 * DAY, last_active_at=NOW - 30 * DAY)
    assert expiry_state(far, NOW)[0] == "scheduled"
