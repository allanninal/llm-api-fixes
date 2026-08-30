from assistants_shutdown_probe import (SHUTDOWN, access_verdict,
                                       cliff_verdict, days_past, probe_state,
                                       repair_lines)


def series(before=1000.0, after=0.0, last_live="2026-08-25"):
    days = ["2026-08-22", "2026-08-23", "2026-08-24", "2026-08-25",
            "2026-08-26", "2026-08-27", "2026-08-28"]
    out = []
    for d in days:
        if d < SHUTDOWN:
            out.append((d, before if d <= last_live else 0.0))
        else:
            out.append((d, after))
    return out


def test_past_the_date_a_200_is_the_finding_and_a_404_is_the_baseline():
    # The inversion this note exists for. Everywhere else in the section a 404
    # is the alarm; here it is the expected answer.
    state, why = access_verdict("gone", "answering", days_past("2026-08-31"))
    assert state == "shut-down"
    assert SHUTDOWN in why

    state, why = access_verdict("answering", "answering", days_past("2026-08-31"))
    assert state == "grace-access"
    assert "grace rather than a supported state" in why
    assert days_past("2026-08-31") == 5
    assert days_past("2026-08-20") == -6


def test_a_dead_control_path_can_never_produce_a_shutdown_verdict():
    # Without this the script reports a closure it has not observed every time
    # somebody runs it with a revoked key.
    state, why = access_verdict("gone", "credentials", 5)
    assert state == "control-failed"
    assert "proves nothing" in why
    assert access_verdict("gone", "unreachable", 5)[0] == "control-failed"
    assert any("re-run" in line for line in repair_lines("control-failed"))


def test_a_429_is_a_refusal_from_a_path_that_still_exists():
    state, why = probe_state(429, {"error": {"code": "rate_limit_exceeded"}})
    assert state == "throttled"
    assert "still routes" in why
    assert access_verdict("throttled", "answering", 5)[0] == "grace-access"
    assert probe_state(200, {"object": "list"})[0] == "answering"
    assert probe_state(404, {"error": {"code": "model_not_found"}})[0] == "gone"
    assert probe_state(None)[0] == "unreachable"
    assert probe_state(500, {})[0] == "refused"


def test_a_cliff_that_lands_on_the_date_is_the_shutdown():
    state, why = cliff_verdict(series())
    assert state == "cliff-on-the-date"
    assert "not a deploy" in why
    assert any("Migrate this project first" in line
               for line in repair_lines(state))


def test_a_cliff_two_days_early_is_a_deploy_and_is_named_as_one():
    state, why = cliff_verdict(series(last_live="2026-08-23"))
    assert state == "cliff-elsewhere"
    assert "2026-08-23" in why
    assert repair_lines(state) == []


def test_a_partial_drop_is_reported_as_a_dip_and_never_rounded_up():
    # A project that served other work as well loses part of its traffic. That
    # is weaker evidence, so it gets its own state and its own sentence.
    state, why = cliff_verdict(series(after=180.0))
    assert state == "dip-on-the-date"
    assert "18%" in why
    assert "part was not" in why
    assert cliff_verdict(series(after=900.0))[0] == "still-running"
    assert cliff_verdict([])[0] == "not-checked"
    assert cliff_verdict([("2026-08-01", 5)])[0] == "window-too-short"
    assert cliff_verdict(series(before=0.0))[0] == "no-traffic-in-window"


def test_the_repair_describes_a_rewrite_and_names_no_model_id():
    lines = repair_lines("shut-down")
    joined = " ".join(lines)
    assert "/v1/responses" in joined
    assert "/v1/conversations" in joined
    assert "assistants=v2" in joined
    assert "no successor model id" in joined
    assert "gpt-" not in joined
