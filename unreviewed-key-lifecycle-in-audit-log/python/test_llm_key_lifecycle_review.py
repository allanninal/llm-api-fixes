import datetime as dt

from llm_key_lifecycle_review import (failed_login_bursts, feed_state, grade,
                                      hour_of, iso, normalise_anthropic,
                                      normalise_openai, parse_when,
                                      project_caveat, resolve_actor, watermark)

ROSTER = {"dana@example.test", "marco@example.test"}
COUNTRIES = ["US", "GB"]


def at(text):
    return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def session_entry(event_type, when, email, ip="198.51.100.24", country="US"):
    return {"id": "audit_1", "type": event_type, "effective_at": when,
            "project": {"id": "proj_1", "name": "prod"},
            "actor": {"type": "session",
                      "session": {"user": {"email": email}, "ip_address": ip,
                                  "ip_address_details": {"country": country}}}}


def test_a_key_minted_at_2am_by_somebody_who_has_left_trips_three_rules():
    # The note in one assertion. Every reason is reported, and the state is the
    # most severe of them, because a reviewer wants all three and triage wants
    # one.
    event = normalise_openai(session_entry(
        "api_key.created", at("2026-03-17T02:14:08Z"), "ada@example.test",
        country="NL"))
    state, reasons = grade(event, ROSTER, (7, 19), COUNTRIES)
    assert state == "off-roster-actor"
    assert len(reasons) == 3
    assert any("not on the current roster" in r for r in reasons)
    assert any("outside the operating geographies" in r for r in reasons)
    assert any("02:00 UTC" in r for r in reasons)
    assert iso(event["when"]) == "2026-03-17T02:14:08Z"


def test_an_empty_feed_is_unavailable_and_never_clean():
    # Audit logging is gated. Reading silence as "no findings" turns a missing
    # control into a passing check, which is the worst outcome available here.
    empty_state, empty_detail = feed_state([], True)
    assert empty_state == "feed-unavailable"
    assert "not a clean result" in empty_detail
    unreachable_state, unreachable_detail = feed_state([], False)
    assert unreachable_state == "feed-unavailable"
    assert "could not be read" in unreachable_detail
    ok_state, ok_detail = feed_state([{"type": "api_key.created"}], True)
    assert ok_state == "feed-readable"
    assert "1 event(s)" in ok_detail


def test_the_two_openai_actor_shapes_keep_their_email_in_different_places():
    session = normalise_openai(session_entry(
        "api_key.created", at("2026-08-11T10:02:00Z"), "Dana@Example.test"))
    assert session["actor_kind"] == "session"
    assert session["actor_email"] == "dana@example.test"
    assert session["country"] == "US"
    assert grade(session, ROSTER, (7, 19), COUNTRIES)[0] == "reviewed"

    by_key = normalise_openai({
        "type": "api_key.deleted", "effective_at": at("2026-08-02T11:40:55Z"),
        "project": {"id": "proj_default"},
        "actor": {"type": "api_key", "api_key": {"id": "key_track",
                                                 "service_account": {"id": "svc_1"}}}})
    assert by_key["actor_kind"] == "api_key"
    assert by_key["actor_email"] is None
    assert resolve_actor(by_key, ROSTER) == "unattributable"
    state, reasons = grade(by_key, ROSTER, (7, 19), COUNTRIES)
    assert state == "unattributable"
    assert any("no user email" in r for r in reasons)
    assert "default project" in project_caveat(by_key)
    assert project_caveat(session) is None


def test_an_anthropic_activity_has_no_country_so_the_rule_is_skipped():
    event = normalise_anthropic({
        "type": "api_key.created", "created_at": "2026-08-14T09:31:00Z",
        "organization_id": "org_1",
        "actor": {"email_address": "MARCO@example.test", "user_id": "u_1",
                  "ip_address": "203.0.113.9", "user_agent": "curl/8"}})
    assert event["source"] == "anthropic"
    assert event["country"] is None
    assert event["actor_email"] == "marco@example.test"
    # The country rule cannot run, so an on-roster in-hours event is clean
    # rather than being failed for a field the feed does not have.
    assert grade(event, ROSTER, (7, 19), COUNTRIES)[0] == "reviewed"
    assert project_caveat(event) is None
    anonymous = normalise_anthropic({"type": "api_key.deleted",
                                     "created_at": "2026-08-14T09:31:00Z"})
    assert anonymous["actor_email"] is None
    assert resolve_actor(anonymous, ROSTER) == "unattributable"


def test_timestamps_arrive_in_two_shapes_and_the_hour_is_utc():
    assert parse_when(1_772_000_000) == 1_772_000_000
    assert parse_when("2026-03-17T02:14:08Z") == at("2026-03-17T02:14:08Z")
    assert parse_when("1772000000") == 1_772_000_000
    assert parse_when(None) is None
    assert parse_when(True) is None
    assert parse_when("whenever") is None
    assert hour_of({"when": at("2026-03-17T02:14:08Z")}) == 2
    assert hour_of({}) is None
    assert iso(None) == "(no timestamp)"


def test_a_burst_of_failed_logins_and_the_watermark_for_the_next_run():
    base = at("2026-08-20T09:00:00Z")
    events = [{"type": "login.failed", "when": base + i * 60,
               "actor_email": "ada@example.test"} for i in range(6)]
    events.append({"type": "api_key.created", "when": base + 4000,
                   "actor_email": "dana@example.test"})
    bursts = failed_login_bursts(events)
    assert len(bursts) == 1
    when, count, who = bursts[0]
    assert when == base and count >= 5 and who == "ada@example.test"
    # One failure is a typo, not a burst.
    assert failed_login_bursts([events[0]]) == []
    assert failed_login_bursts([]) == []
    assert watermark(events) == base + 4000
    assert watermark([]) is None
    assert watermark([{"type": "x"}]) is None


def test_an_out_of_hours_read_event_is_not_a_creation():
    # The out-of-hours rule fires on lifecycle events, not on everything that
    # happens to be timestamped at night.
    updated = {"source": "openai", "type": "api_key.updated",
               "when": at("2026-08-20T03:00:00Z"), "actor_kind": "session",
               "actor_email": "dana@example.test", "actor_ip": "203.0.113.1",
               "country": "US"}
    assert grade(updated, ROSTER, (7, 19), COUNTRIES)[0] == "reviewed"
    created = dict(updated, type="service_account.created")
    state, reasons = grade(created, ROSTER, (7, 19), COUNTRIES)
    assert state == "out-of-hours"
    assert reasons == ["created outside business hours (03:00 UTC)"]
