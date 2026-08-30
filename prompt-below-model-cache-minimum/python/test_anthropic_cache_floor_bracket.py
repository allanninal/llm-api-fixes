from anthropic_cache_floor_bracket import (by_key, cache_minimum, classify,
                                           floor_bracket, handoff,
                                           models_caching_anywhere, repair_lines,
                                           series, split_rows)


def model(name, uncached=5_000_000, writes=0, reads=0):
    return {"model": name, "floor": cache_minimum(name), "uncached": uncached,
            "writes": writes, "reads": reads}


# One key, one prompt, four models. Caching works at 512 and 1,024 and stops
# dead at 2,048 and 4,096. The prefix is therefore between 1,024 and 2,048.
BRACKETED = [
    model("claude-opus-5", writes=2_000_000, reads=9_000_000),
    model("claude-sonnet-5-20260115", writes=1_500_000, reads=7_000_000),
    model("claude-haiku-3-5"),
    model("claude-haiku-4-5-20251001"),
]


def test_the_bracket_is_the_finding():
    # The note in one assertion: a size derived from a report with no request
    # count and no prompt in it, purely from where caching stops working.
    state, detail = classify(BRACKETED)
    assert state == "below-cache-minimum"
    assert "caching works up to a floor of 1024" in detail
    assert "stops at 2048" in detail
    assert "at least 1024 tokens and under 2048" in detail

    caching, silent, _ = split_rows(BRACKETED)
    assert floor_bracket(caching, silent) == (1024, 2048)
    assert handoff(state) == ""


def test_a_dated_snapshot_resolves_to_its_family_floor():
    # The ids in a usage report are dated. A floor lookup that misses them
    # reports every real organization as unrecognised.
    assert cache_minimum("claude-haiku-4-5-20251001") == 4096
    assert cache_minimum("claude-sonnet-4-5-20250929") == 1024
    assert cache_minimum("claude-opus-5") == 512
    assert cache_minimum("claude-fable-5") == 512
    # Longest prefix wins: opus-4 and opus-4-5 have different floors and the
    # shorter family must not swallow the longer one.
    assert cache_minimum("claude-opus-4-5-20251101") == 4096
    assert cache_minimum("claude-opus-4-20250514") == 1024
    assert cache_minimum("gpt-5.6") is None
    assert cache_minimum("") is None


def test_an_unknown_floor_is_never_treated_as_zero():
    # A model with no floor on the caching side would drag lo down to 0 and
    # invent a bracket of 0 to 4096, which is not a finding, it is a shrug.
    rows = [model("claude-future-9", writes=1_000_000, reads=5_000_000),
            model("claude-haiku-4-5")]
    caching, silent, skipped = split_rows(rows)
    assert [r["model"] for r in skipped] == ["claude-future-9"]
    assert caching == []
    state, _ = classify(rows)
    assert state == "single-silent-model"


def test_a_silent_model_under_a_caching_floor_is_someone_elses_note():
    # opus-5 needs 512 and is silent while haiku-4-5 needs 4,096 and caches.
    # The prompt cleared the higher bar, so size cannot be the explanation.
    rows = [model("claude-opus-5"),
            model("claude-haiku-4-5", writes=2_000_000, reads=8_000_000)]
    state, detail = classify(rows)
    assert state == "silent-model-under-a-caching-floor"
    assert "claude-opus-5 (floor 512) is silent" in detail
    assert "cache-invalidated-by-changing-prefix" in handoff(state)
    caching, silent, _ = split_rows(rows)
    assert floor_bracket(caching, silent) is None


def test_no_caching_at_all_is_the_never_switched_on_note():
    rows = [model("claude-opus-5"), model("claude-haiku-4-5")]
    state, detail = classify(rows)
    assert state == "no-caching-anywhere"
    assert "silent on all 2 model(s)" in detail
    assert "prompt-caching-never-used" in handoff(state)


def test_one_silent_model_is_ambiguous_and_says_so():
    # No contrast, no bracket. This is the case the note refuses to claim.
    state, detail = classify([model("claude-haiku-4-5")])
    assert state == "single-silent-model"
    assert "no second floor to bracket against" in detail
    note = handoff(state)
    assert "prompt-caching-never-used" in note and "remain open" in note


def test_a_peer_key_caching_the_same_model_clears_the_model():
    peers = {"claude-haiku-4-5"}
    state, detail = classify([model("claude-haiku-4-5")], peers)
    assert state == "peer-caches-same-model"
    assert "another key caches on the same model" in detail
    assert "cache-invalidated-by-changing-prefix" in handoff(state)


def test_a_thin_silent_model_is_not_evidence():
    # Silence on a model that barely ran proves nothing, and must not be the
    # hi end of a bracket.
    rows = [model("claude-opus-5", writes=1_000_000, reads=4_000_000),
            model("claude-haiku-4-5", uncached=900)]
    caching, silent, skipped = split_rows(rows)
    assert silent == [] and len(skipped) == 1
    assert classify(rows)[0] == "caches-on-every-model"


def test_the_report_is_folded_into_keys_and_models():
    buckets = [{"starting_at": "2026-08-%02dT00:00:00Z" % day,
                "results": [
                    {"api_key_id": "apikey_01Ab", "model": "claude-opus-5",
                     "uncached_input_tokens": 1_000_000,
                     "cache_read_input_tokens": 4_000_000,
                     "cache_creation": {"ephemeral_5m_input_tokens": 500_000,
                                        "ephemeral_1h_input_tokens": 0}},
                    {"api_key_id": "apikey_01Ab", "model": "claude-haiku-4-5",
                     "uncached_input_tokens": 3_000_000,
                     "cache_read_input_tokens": 0,
                     "cache_creation": {}},
                ]} for day in range(1, 6)]
    totals = series(buckets)
    assert totals[("apikey_01Ab", "claude-opus-5")]["reads"] == 20_000_000
    assert totals[("apikey_01Ab", "claude-haiku-4-5")]["writes"] == 0
    assert models_caching_anywhere(totals) == {"claude-opus-5"}

    keyed = by_key(totals)
    rows = keyed["apikey_01Ab"]
    assert [r["floor"] for r in rows] == [512, 4096]
    state, _ = classify(rows)
    assert state == "below-cache-minimum"
    assert any("4096 tokens" in line for line in repair_lines((512, 4096)))


def test_empty_and_unreadable_input_produce_no_verdict():
    assert classify([])[0] == "too-little-traffic"
    assert classify(None)[0] == "too-little-traffic"
    assert series([]) == {}
    assert series([{"results": [None, "nonsense"]}]) == {}
    assert by_key({}) == {}
    assert floor_bracket([], []) is None
    assert repair_lines(None) == []
