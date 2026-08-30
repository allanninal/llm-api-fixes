from openai_response_chain_probe import (RETENTION_DAYS, age_days,
                                        classify_chain, link_row, oldest_link,
                                        parse_ids, repair_lines, runway_days)

NOW = 1_800_000_000
DAY = 86400


def link(rid, days_old, parent="", conversation=""):
    return link_row({"id": rid, "created_at": NOW - int(days_old * DAY),
                     "previous_response_id": parent,
                     "conversation": ({"id": conversation} if conversation
                                      else None),
                     "status": "completed"})


def test_a_missing_parent_is_the_finding_and_names_the_next_turn():
    chain = [link("resp_c9", 1.0, parent="resp_a1")]
    state, detail = classify_chain("resp_c9", chain, "resp_a1", "", False,
                                   NOW, 5.0)
    assert state == "chain-broken"
    assert "resp_a1 no longer resolves" in detail
    assert "next turn on this thread will 404" in detail
    lines = repair_lines(state)
    assert any("replaying local history" in line for line in lines)
    assert any("no 30 day TTL" in line for line in lines)

    # The head itself missing is the same verdict with a different sentence,
    # because a 404 there has two causes and the script names both.
    state, detail = classify_chain("resp_c9", [], "resp_c9", "", False, NOW, 5.0)
    assert state == "chain-broken"
    assert "aged out" in detail and "never stored" in detail


def test_the_runway_comes_from_the_oldest_link_and_not_the_newest():
    chain = [link("resp_f2", 0.5, parent="resp_e1"),
             link("resp_e1", 12.0, parent="resp_d7"),
             link("resp_d7", 26.4)]
    assert oldest_link(chain)["id"] == "resp_d7"
    assert abs(runway_days(chain, NOW) - (RETENTION_DAYS - 26.4)) < 0.01
    state, detail = classify_chain("resp_f2", chain, "", "", False, NOW, 5.0)
    assert state == "chain-expiring"
    assert "26.4 days old" in detail and "3.6 days" in detail
    # Read from the newest link this chain looks like it has 29.5 days left.
    assert age_days(chain[0]["created_at"], NOW) < 1.0


def test_a_conversation_backed_chain_is_not_this_note():
    chain = [link("resp_k4", 1.0, parent="resp_k3", conversation="conv_x1"),
             link("resp_k3", 44.0, conversation="conv_x1")]
    state, detail = classify_chain("resp_k4", chain, "", "", False, NOW, 5.0)
    assert state == "conversation-backed"
    assert "no 30 day TTL" in detail
    assert repair_lines(state) == []
    # One link without a conversation is not a conversation backed chain.
    mixed = [chain[0], link("resp_k3", 44.0)]
    assert classify_chain("resp_k4", mixed, "", "", False, NOW, 5.0)[0] \
        == "chain-broken"


def test_a_chain_cut_short_by_the_hop_limit_is_not_graded_healthy():
    chain = [link("resp_z9", 1.0, parent="resp_z8"),
             link("resp_z8", 2.0, parent="resp_z7")]
    state, detail = classify_chain("resp_z9", chain, "", "", True, NOW, 5.0)
    assert state == "chain-unfinished"
    assert "oldest link was never seen" in detail
    assert any("--max-hops" in line for line in repair_lines(state))
    # The same chain walked to a root is intact.
    rooted = [chain[0], link("resp_z8", 2.0)]
    assert classify_chain("resp_z9", rooted, "", "", False, NOW, 5.0)[0] \
        == "chain-intact"


def test_link_row_reads_four_fields_and_invents_no_store_flag():
    row = link_row({"id": "resp_a1", "created_at": 1700000000,
                    "previous_response_id": None,
                    "conversation": {"id": "conv_x1"}, "status": "completed"})
    assert row == {"id": "resp_a1", "created_at": 1700000000,
                   "previous_response_id": "", "conversation": "conv_x1",
                   "status": "completed"}
    assert "store" not in row and "stored" not in row
    assert link_row(None)["id"] == ""
    assert link_row({"created_at": "nonsense"})["created_at"] == 0
    assert age_days(0, NOW) is None and runway_days([], NOW) is None


def test_the_id_file_is_read_the_way_it_is_actually_exported():
    ids = parse_ids("resp_a1\n\n# heads exported 2026-08-30\nresp_b2  # oldest\n"
                    "resp_a1\n   \nresp_c3\n")
    assert ids == ["resp_a1", "resp_b2", "resp_c3"]
    assert parse_ids("") == [] and parse_ids(None) == []
    state, detail = classify_chain("resp_a1", [], "", "HTTP 403 reading resp_a1",
                                   False, NOW, 5.0)
    assert state == "chain-unreadable"
    assert "nothing about this chain was established" in detail
