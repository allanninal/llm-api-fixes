import datetime as dt

from openai_spend_limit_audit import (projected_month_end, threshold_dollars,
                                      unknown_recipients, verdict)

# The 15th of a 31-day month, so a little under half of it has elapsed.
NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)


def limit_of(cents, status="enforcing"):
    return {"object": "organization.spend_limit", "threshold_amount": cents,
            "currency": "USD", "interval": "month",
            "enforcement": {"status": status}}


def alert(cents, recipients=("oncall@example.com",)):
    return {"object": "organization.spend_alert", "threshold_amount": cents,
            "notification_channel": {"type": "email",
                                     "recipients": list(recipients)}}


def test_threshold_is_cents_not_dollars():
    assert threshold_dollars(limit_of(90000)) == 900.0
    assert threshold_dollars({"spend_limit": limit_of(50000)}) == 500.0
    assert threshold_dollars({}) is None
    assert threshold_dollars(None) is None
    assert threshold_dollars(limit_of("not a number")) is None


def test_projection_pro_rates_against_the_clock_it_is_given():
    # 14.5 days of a 31 day month have elapsed, so spend roughly doubles.
    assert round(projected_month_end(1000.0, NOW)) == 2138
    # The first hour of the month must not divide by zero or project infinity.
    first = dt.datetime(2026, 8, 1, 0, 0, tzinfo=dt.timezone.utc)
    assert round(projected_month_end(10.0, first)) == 10 * 31 * 24


def test_no_limit_at_all_is_the_headline_finding():
    state, detail = verdict({}, [], 400.0, NOW)
    assert state == "no-limit"
    assert "no spend limit is configured" in detail


def test_a_limit_that_is_not_enforcing_is_reported_before_any_arithmetic():
    # An inactive limit has the same effect on the bill as no limit, and
    # comparing it against the run rate would describe a brake that is not
    # connected to anything.
    state, _ = verdict(limit_of(90000, status="inactive"), [alert(45000)], 400.0, NOW)
    assert state == "not-enforcing"


def test_a_threshold_typed_as_dollars_is_named_as_the_cents_mistake():
    # 500 meaning five hundred dollars is five dollars.
    state, detail = verdict(limit_of(500), [alert(250)], 400.0, NOW)
    assert state == "cents-mistake"
    assert "in cents" in detail


def test_already_over_and_on_track_to_go_over_are_different_states():
    assert verdict(limit_of(30000), [alert(15000)], 400.0, NOW)[0] == "breached"
    assert verdict(limit_of(70000), [alert(35000)], 400.0, NOW)[0] == "will-breach"


def test_a_ceiling_far_above_the_run_rate_cannot_fire_in_time():
    state, detail = verdict(limit_of(5000000), [alert(2500000)], 400.0, NOW)
    assert state == "ceiling-too-high"
    assert "five times" in detail


def test_a_brake_with_no_warning_light_is_its_own_finding():
    state, detail = verdict(limit_of(200000), [], 400.0, NOW)
    assert state == "no-alerts"
    assert "429" in detail


def test_a_limit_and_alerts_together_is_guarded():
    state, detail = verdict(limit_of(200000), [alert(100000), alert(150000)],
                            400.0, NOW)
    assert state == "guarded"
    assert "2 alert(s)" in detail


def test_recipients_who_left_are_not_an_alert():
    alerts = [alert(1000, ("oncall@example.com", "Departed@Example.com")),
              alert(2000, ("oncall@example.com",))]
    assert unknown_recipients(alerts, ["OnCall@example.com"]) == ["Departed@Example.com"]
    assert unknown_recipients(alerts, ["oncall@example.com", "departed@example.com"]) == []
