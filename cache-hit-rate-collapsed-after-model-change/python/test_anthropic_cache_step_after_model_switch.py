from anthropic_cache_step_after_model_switch import (arrival_positions,
                                                     best_split, cache_minimum,
                                                     classify, daily_rows,
                                                     day_key, floor_note,
                                                     handoff, input_share_after,
                                                     previous_model,
                                                     repair_lines, step_at,
                                                     sustained)

OLD = "claude-opus-5"
NEW = "claude-haiku-4-5-20251001"


def day(position, share, models):
    """One day of the org-wide report. models maps id to input tokens."""
    total = sum(models.values())
    reads = int(round(total * share))
    return {"day": "2026-08-%02d" % (position + 1), "position": position,
            "share": share, "reads": reads, "uncached": total - reads,
            "writes": 0, "by_model": dict(models)}


def switched(before=0.70, cold=0.20, after=0.10, at=15, new_share=1.0,
             days=31):
    """A migration on day `at`, with the new model taking `new_share` of input."""
    rows = []
    for position in range(days):
        if position < at:
            rows.append(day(position, before, {OLD: 40_000_000}))
        else:
            mix = {NEW: int(40_000_000 * new_share)}
            if new_share < 1.0:
                mix[OLD] = 40_000_000 - mix[NEW]
            rows.append(day(position, cold if position == at else after, mix))
    return rows


STEP = switched()


def test_the_step_aligned_with_the_arrival_is_the_finding():
    # The note in one assertion. The switch day itself is thrown away, because
    # a cold cache on day one of a new model is correct behaviour.
    assert arrival_positions(STEP) == {NEW: 15}
    assert round(input_share_after(STEP, NEW, 15), 3) == 1.0

    shares = [r["share"] for r in STEP]
    before, after, delta = step_at(shares, 15)
    assert round(before, 2) == 0.70 and round(after, 2) == 0.10
    assert round(delta, 2) == 0.60
    assert best_split(shares)[0] == 15
    assert sustained(shares, 15) is True

    state, detail = classify(STEP)
    assert state == "collapsed-after-model-change"
    assert "70% before claude-haiku-4-5-20251001 arrived on 2026-08-16" in detail
    assert "10% after, with the switch day itself excluded" in detail
    assert "largest step in the window is exactly there" in detail
    assert handoff(state) == ""


def test_a_dip_that_recovers_is_the_cold_cache_doing_its_job():
    # Identical arrival, identical switch-day dip, opposite verdict. This is
    # the case that excluding the switch day exists for.
    recovered = switched(before=0.70, cold=0.20, after=0.70)
    assert [r["share"] for r in recovered][15] == 0.20
    state, detail = classify(recovered)
    assert state == "expected-cold-start"
    assert "dipped to 20% that day and settled back at 70%" in detail
    assert "not a finding" in handoff(state)


def test_a_collapse_somewhere_else_is_not_the_switch():
    # The new model arrives on day 5 and the share holds for another fortnight
    # before falling off a cliff. Alignment is what makes the claim falsifiable.
    rows = []
    for position in range(31):
        models = {OLD: 40_000_000} if position < 5 else {NEW: 40_000_000}
        rows.append(day(position, 0.70 if position < 20 else 0.10, models))
    shares = [r["share"] for r in rows]
    assert arrival_positions(rows) == {NEW: 5}
    assert best_split(shares)[0] == 20

    state, detail = classify(rows)
    assert state == "step-elsewhere"
    assert "falls hardest at 2026-08-21" in detail
    assert "cache-invalidated-by-changing-prefix" in handoff(state)


def test_a_canary_model_is_never_blamed():
    # A new id carrying three percent of input cannot move an org-wide ratio,
    # and letting it take the blame points at the wrong deploy.
    rows = switched(new_share=0.03)
    assert round(input_share_after(rows, NEW, 15), 2) == 0.03
    state, detail = classify(rows)
    assert state == "new-model-marginal"
    assert "carries only 3% of input since" in detail


def test_a_window_with_no_new_model_makes_no_claim():
    rows = [day(p, 0.70 if p < 15 else 0.10, {OLD: 40_000_000}) for p in range(31)]
    assert arrival_positions(rows) == {}
    state, detail = classify(rows)
    assert state == "no-new-model"
    assert "already present on day one" in detail
    assert "cache-invalidated-by-changing-prefix" in handoff(state)


def test_a_share_that_holds_across_the_switch_is_steady():
    state, detail = classify(switched(before=0.70, cold=0.70, after=0.70))
    assert state == "steady"
    assert "held at 70% against 70% before" in detail


def test_a_recovery_after_the_step_is_only_suggestive():
    rows = switched()
    rows[28]["share"] = 0.90
    state, detail = classify(rows)
    assert state == "partial-recovery"
    assert "recovered above the pre-switch floor" in detail
    assert sustained([r["share"] for r in rows], 15) is False


def test_the_floors_explain_the_step_without_making_it():
    assert cache_minimum(NEW) == 4096
    assert cache_minimum(OLD) == 512
    note = floor_note(OLD, NEW)
    assert "needs 4096 tokens" in note
    assert "prompt-below-model-cache-minimum" in note
    # A move to a lower floor gets the opposite sentence, not silence.
    other = floor_note("claude-haiku-4-5", "claude-opus-5")
    assert "does not explain this" in other
    assert floor_note(OLD, "gpt-5.6") == ""
    assert any("thinking" in line for line in repair_lines(OLD, NEW))


def test_the_report_is_folded_into_days_and_models():
    buckets = []
    for position in range(31):
        model = OLD if position < 15 else NEW
        share = 0.70 if position < 15 else (0.20 if position == 15 else 0.10)
        total = 40_000_000
        reads = int(total * share)
        buckets.append({"starting_at": "2026-08-%02dT00:00:00Z" % (position + 1),
                        "results": [{"model": model,
                                     "uncached_input_tokens": total - reads,
                                     "cache_read_input_tokens": reads,
                                     "cache_creation": {
                                         "ephemeral_5m_input_tokens": 0,
                                         "ephemeral_1h_input_tokens": 0}}]})
    rows = daily_rows(buckets)
    assert len(rows) == 31
    assert [r["position"] for r in rows] == list(range(31))
    assert round(rows[0]["share"], 2) == 0.70
    assert previous_model(rows, 15) == OLD
    assert classify(rows)[0] == "collapsed-after-model-change"


def test_thin_and_unreadable_windows_produce_no_verdict():
    assert classify([day(p, 0.5, {OLD: 1000}) for p in range(5)])[0] == "too-few-days"
    assert classify([])[0] == "too-few-days"
    assert classify(None)[0] == "too-few-days"
    assert step_at([0.1, 0.2], 1) == (None, None, None)
    assert best_split([0.1, 0.2]) == (None, None)
    assert sustained([], 3) is False
    assert input_share_after([], OLD, 0) is None
    assert previous_model([], 3) is None
    assert day_key("nonsense") is None
    assert daily_rows([{"starting_at": "bad", "results": []}]) == []
