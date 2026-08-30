from llm_spend_week_over_week import (classify, daily_from_anthropic,
                                        daily_from_openai, parse_cents, weeks)


def test_today_is_never_counted_in_the_newest_week():
    # Fifteen days of a dollar a day, run on the fifteenth. The last day is
    # partial by definition, so two whole weeks come back and today is not in
    # either of them.
    daily = {"2026-08-%02d" % day: 1.0 for day in range(1, 16)}
    got = weeks(daily, "2026-08-15")
    assert len(got) == 2
    assert got[0] == ("2026-08-08", "2026-08-14", 7.0)
    assert got[1] == ("2026-08-01", "2026-08-07", 7.0)


def test_a_partial_oldest_week_is_dropped_rather_than_reported_short():
    daily = {"2026-08-%02d" % day: 10.0 for day in range(1, 12)}
    got = weeks(daily, "2026-08-12")
    assert [w[2] for w in got] == [70.0]


def test_one_high_week_is_a_spike_and_two_are_a_step():
    spike, detail = classify([3000.0, 1000.0, 1000.0, 1000.0])
    assert spike == "spike"
    assert "a job that ran" in detail

    step, detail = classify([3000.0, 3000.0, 1000.0, 1000.0])
    assert step == "step"
    assert "held for two weeks" in detail


def test_a_ramp_is_caught_even_though_week_over_week_never_trips():
    # +15% a week. Against the mean of the previous three the newest week is
    # only 31% up, so a ratio threshold of 40% would call this flat forever.
    state, detail = classify([1520.88, 1322.5, 1150.0, 1000.0])
    assert state == "ramp"
    assert "already in the baseline" in detail
    assert classify([1000.0, 1000.0, 1000.0, 1000.0])[0] == "flat"


def test_spend_falling_off_a_cliff_is_reported_rather_than_celebrated():
    state, detail = classify([400.0, 1000.0, 1000.0, 1000.0])
    assert state == "drop"
    assert "traffic that stopped" in detail


def test_a_short_history_and_a_standing_start_are_their_own_answers():
    assert classify([5000.0, 10.0])[0] == "too-short"
    assert classify([500.0, 0.0, 0.0])[0] == "new-spend"
    assert classify([0.0, 0.0, 0.0])[0] == "no-spend"
    assert classify(["lots", 1.0, 2.0])[0] == "unreadable"


def test_anthropic_cents_are_parsed_exactly_and_not_as_floats():
    assert parse_cents("1234.5") == 1234500
    assert parse_cents("0.001") == 1
    assert parse_cents("-250") == -250000
    assert parse_cents("") is None
    assert parse_cents(None) is None
    assert parse_cents("1,234") is None
    assert parse_cents("lots") is None


def test_both_providers_fold_into_the_same_day_keyed_dollars():
    # 2026-08-01T00:00:00Z is 1785542400. Two results in one bucket sum.
    openai = daily_from_openai([{
        "start_time": 1785542400, "end_time": 1785628800,
        "results": [{"amount": {"value": 12.5, "currency": "usd"}},
                    {"amount": {"value": 0.25, "currency": "usd"}}]}])
    assert openai == {"2026-08-01": 12.75}

    anthropic = daily_from_anthropic([{
        "starting_at": "2026-08-01T00:00:00Z",
        "results": [{"amount": "1250.0"}, {"amount": "25"}]}])
    assert anthropic == {"2026-08-01": 12.75}
    assert daily_from_anthropic([{"starting_at": "nonsense",
                                  "results": [{"amount": "1"}]}]) == {}
