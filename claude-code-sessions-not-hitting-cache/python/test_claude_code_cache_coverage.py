import datetime as dt

from claude_code_cache_coverage import (actor_name, cost_cents,
                                        cost_per_session, day_strings, fold,
                                        mask, read_share, repair_lines,
                                        tokens_of, verdict)


def breakdown(model, input_tokens, cache_read=0, cache_creation=0, cents="0"):
    return {"model": model,
            "tokens": {"input": input_tokens, "output": 12_000,
                       "cache_read": cache_read,
                       "cache_creation": cache_creation},
            "estimated_cost": {"currency": "USD", "amount": cents}}


def record(email, sessions, entries):
    return {"date": "2026-08-30",
            "actor": {"type": "user_actor", "email_address": email},
            "core_metrics": {"num_sessions": sessions,
                             "lines_of_code": {"added": 90, "removed": 12},
                             "commits_by_claude_code": 2},
            "model_breakdown": entries}


def page(records):
    return {"data": records, "has_more": False}


def test_two_developers_on_the_same_work_and_one_never_reads_a_prefix():
    # The note in one assertion. Same repository, same model, same week; the
    # difference is whether a session was continued or restarted.
    rows = fold([page([
        record("nobody@example.com", 11,
               [breakdown("claude-opus-5", 2_000_000, cents="4120")]),
        record("someone@example.com", 4,
               [breakdown("claude-opus-5", 300_000, cache_read=1_600_000,
                          cache_creation=200_000, cents="940")]),
    ])])

    state, detail = verdict(rows["nobody@example.com"])
    assert state == "no-cache-at-all"
    assert "11 session(s), 0%" in detail
    assert any("turns of the same session" in line
               for line in repair_lines(state))

    good, good_detail = verdict(rows["someone@example.com"])
    assert good == "cached"
    assert "84% of input read from cache" in good_detail


def test_a_single_session_zero_is_arithmetic_not_a_finding():
    rows = fold([page([
        record("once@example.com", 1,
               [breakdown("claude-opus-5", 2_000_000, cents="900")])])])
    state, detail = verdict(rows["once@example.com"])
    assert state == "too-few-sessions"
    assert "no earlier turn" in detail
    assert repair_lines(state) == []


def test_written_and_never_matched_is_the_more_expensive_zero():
    rows = fold([page([
        record("churn@example.com", 6,
               [breakdown("claude-opus-5", 900_000, cache_creation=2_100_000,
                          cents="5890")])])])
    state, detail = verdict(rows["churn@example.com"])
    assert state == "writes-never-read"
    assert "2.1M token(s) written" in detail
    assert any("worse than not caching at all" in line
               for line in repair_lines(state))


def test_the_whole_model_breakdown_is_summed_and_not_just_the_first_entry():
    rows = fold([page([
        record("two@example.com", 5, [
            breakdown("claude-opus-5", 1_000_000, cache_read=500_000, cents="1000"),
            breakdown("claude-haiku-4-5-20251001", 400_000, cache_read=100_000,
                      cents="250"),
        ])])])
    row = rows["two@example.com"]
    assert row["input"] == 1_400_000
    assert row["cache_read"] == 600_000
    assert row["cents"] == 1250
    assert row["models"] == {"claude-opus-5", "claude-haiku-4-5-20251001"}
    assert float(cost_per_session(row)) == 250.0


def test_sessions_and_cost_accumulate_across_days():
    day = [record("daily@example.com", 3,
                  [breakdown("claude-opus-5", 500_000, cents="600")])]
    rows = fold([page(day), page(day), page(day)])
    assert rows["daily@example.com"]["sessions"] == 9
    assert rows["daily@example.com"]["days"] == 3
    assert rows["daily@example.com"]["cents"] == 1800


def test_both_actor_shapes_are_read_and_neither_is_handled():
    assert actor_name({"actor": {"type": "user_actor",
                                 "email_address": "a@example.com"}}) == \
        "a@example.com"
    assert actor_name({"actor": {"type": "api_actor",
                                 "api_key_name": "ci-runner"}}) == "ci-runner"
    assert actor_name({"actor": {}}) == "unattributed"
    assert actor_name({}) == "unattributed"
    assert actor_name(None) == "unattributed"


def test_an_email_address_is_masked_before_it_is_printed():
    assert mask("someone@example.com") == "s***@example.com"
    assert mask("ci-runner") == "ci-runner"
    assert mask("") == "unattributed"
    assert mask(None) == "unattributed"


def test_reads_over_reads_plus_input_and_writes_are_not_a_hit():
    assert read_share({"cache_read": 900, "input": 100}) == 0.9
    # A prefix written and never matched is not partly cached.
    assert read_share({"cache_read": 0, "input": 100, "cache_creation": 900}) == 0.0
    assert read_share({}) == 0.0
    assert cost_per_session({"sessions": 0, "cents": 100}) is None
    assert tokens_of(None) == {"input": 0, "output": 0, "cache_read": 0,
                               "cache_creation": 0}
    assert tokens_of({"tokens": {"input": "x"}})["input"] == 0
    assert cost_cents({"estimated_cost": {"amount": "12.50"}}) == 12.5
    assert cost_cents({"estimated_cost": {"amount": "not money"}}) == 0


def test_today_is_never_requested_because_today_is_always_partial():
    days = day_strings(3, dt.date(2026, 8, 31))
    assert days == ["2026-08-30", "2026-08-29", "2026-08-28"]
    assert day_strings(1, dt.date(2026, 1, 1)) == ["2025-12-31"]
    assert fold([]) == {} and fold(None) == {}
