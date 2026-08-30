from anthropic_wall_clock_preflight import (duration, generation_seconds,
                                             prefill_seconds, safe_max_tokens,
                                             timeout_seconds, unit_suspicion,
                                             verdict)


def test_the_transport_decides_it_and_the_prompt_does_not():
    # A two thousand token prompt asking for 64,000 tokens back.
    seconds = prefill_seconds(2_000) + generation_seconds(64_000)
    assert duration(seconds) == "19m 23s"

    state, detail = verdict(seconds, streams=False)
    assert state == "over-wall-clock-not-streaming"
    assert "504" in detail
    assert "Raising the client timeout does not move it" in detail

    # Same seconds, same prompt, streaming: not a finding at all.
    state, detail = verdict(seconds, streams=True)
    assert state == "streams-past-ten-minutes"
    assert "never goes idle" in detail


def test_an_enormous_prompt_with_a_small_answer_is_quick():
    # The mirror image, and the reason this script reports prefill separately:
    # thirty times the input, a twentieth of the time.
    seconds = prefill_seconds(60_000) + generation_seconds(1_024)
    assert duration(seconds) == "0m 28s"
    assert verdict(seconds, streams=False)[0] == "within-budget"


def test_the_models_own_cap_is_forty_minutes_of_generation():
    # Legal to request, impossible to deliver on a non-streaming call.
    assert duration(generation_seconds(128_000)) == "38m 47s"
    assert safe_max_tokens() == 33_000
    assert safe_max_tokens(55.0, 600.0, prefill=100.0) == 27_500
    assert safe_max_tokens(tps=0) == 0


def test_six_hundred_means_two_different_things_in_two_sdks():
    assert timeout_seconds("python", 600) == 600.0
    assert timeout_seconds("ruby", 600) == 600.0
    assert timeout_seconds("typescript", 600) == 0.6
    assert timeout_seconds("TypeScript", 600) == 0.6
    assert unit_suspicion("typescript", 600) is True
    assert unit_suspicion("node", 600) is True
    assert unit_suspicion("python", 600) is False
    # A deliberate ten minutes on the TypeScript client is not suspicious.
    assert unit_suspicion("typescript", 600_000) is False
    # An SDK this script does not know about gets no guess at all.
    assert timeout_seconds("rust", 600) is None
    assert unit_suspicion("rust", 600) is False
    assert timeout_seconds("python", None) is None


def test_the_wall_clock_is_reported_ahead_of_the_client_timeout():
    # Both are true for this path. The wall clock is the one that matters,
    # because raising the client timeout leaves the request failing.
    state, _ = verdict(1_200, streams=False, timeout_s=300.0)
    assert state == "over-wall-clock-not-streaming"
    # Streaming removes the wall clock, and then the client timeout is the
    # binding number.
    state, detail = verdict(1_200, streams=True, timeout_s=300.0)
    assert state == "over-client-timeout"
    assert "gives up before the API is finished" in detail


def test_a_path_close_to_the_ceiling_is_reported_before_it_crosses():
    state, detail = verdict(540, streams=False)
    assert state == "near-wall-clock-not-streaming"
    assert "inside 80% of the 10m 00s ceiling" in detail
    assert verdict(400, streams=False)[0] == "within-budget"


def test_durations_read_as_minutes_and_seconds():
    assert duration(0) == "0m 00s"
    assert duration(59.9) == "0m 59s"
    assert duration(600) == "10m 00s"
    assert duration(-5) == "0m 00s"
    assert duration(None) == "0m 00s"
