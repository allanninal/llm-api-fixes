from anthropic_batch_queue_depth import (enqueued_limit, headroom, queue_depth,
                                        queue_rows, repair_lines, top_holders,
                                        verdict, workspace_keys)

RATE_LIMITS = {
    "data": [
        {"type": "rate_limit", "group_type": "model_group",
         "models": ["claude-opus-5"],
         "limits": [{"type": "requests_per_minute", "value": 4000},
                    {"type": "input_tokens_per_minute", "value": 10000000}]},
        {"type": "rate_limit", "group_type": "batch", "models": None,
         "limits": [{"type": "enqueued_batch_requests", "value": 300000}]},
    ],
    "next_page": None,
}

BATCHES = [
    {"id": "msgbatch_01Rf", "processing_status": "in_progress",
     "request_counts": {"processing": 214900, "succeeded": 0, "errored": 0,
                        "canceled": 0, "expired": 0}},
    {"id": "msgbatch_01Qa", "processing_status": "in_progress",
     "request_counts": {"processing": 58400, "succeeded": 0, "errored": 0,
                        "canceled": 0, "expired": 0}},
    {"id": "msgbatch_01Zc", "processing_status": "canceling",
     "request_counts": {"processing": 9600, "succeeded": 200, "errored": 0,
                        "canceled": 0, "expired": 0}},
    {"id": "msgbatch_01Done", "processing_status": "ended",
     "request_counts": {"processing": 0, "succeeded": 50000, "errored": 0,
                        "canceled": 0, "expired": 0}},
]


def test_the_ceiling_comes_out_of_the_batch_group_and_nowhere_else():
    assert enqueued_limit(RATE_LIMITS) == 300000
    # Absent means unknown, never zero: a zero ceiling reads as infinite
    # occupancy and alarms on an empty queue.
    assert enqueued_limit({"data": [RATE_LIMITS["data"][0]]}) is None
    assert enqueued_limit({}) is None
    assert enqueued_limit({"data": [{"group_type": "batch",
                                     "limits": [{"type": "other", "value": 1}]}]}) is None
    assert enqueued_limit({"data": [{"group_type": "batch",
                                     "limits": [{"type": "enqueued_batch_requests",
                                                 "value": "lots"}]}]}) is None


def test_only_live_batches_and_only_the_processing_count_are_the_queue():
    rows = queue_rows(BATCHES, "ws1")
    assert [r["id"] for r in rows] == ["msgbatch_01Rf", "msgbatch_01Qa",
                                       "msgbatch_01Zc"]
    # An ended batch holds nothing in the queue however many it succeeded on.
    assert all(r["id"] != "msgbatch_01Done" for r in rows)
    # canceling is still live, because those requests have not been processed.
    assert rows[2]["status"] == "canceling"
    assert queue_depth(rows) == 282900
    assert queue_depth([]) == 0


def test_occupancy_is_measured_against_the_threshold_that_was_passed_in():
    rows = queue_rows(BATCHES)
    depth = queue_depth(rows)
    remaining, occupancy = headroom(depth, 300000)
    assert remaining == 17100 and round(occupancy, 3) == 0.943
    state, detail = verdict(depth, 300000, rows, 1, 80)
    assert state == "queue-near-limit" and "94% of the ceiling" in detail
    assert verdict(depth, 300000, rows, 1, 95)[0] == "queue-clear"
    # At or over the ceiling, submissions are refused rather than slowed.
    state, detail = verdict(300000, 300000, rows, 1, 80)
    assert state == "queue-exhausted" and "being refused" in detail
    assert headroom(10, None) == (None, None)
    assert headroom(10, 0) == (None, None)


def test_an_unreadable_ceiling_is_a_finding_with_its_own_repair():
    rows = queue_rows(BATCHES)
    state, detail = verdict(queue_depth(rows), None, rows, 1, 80)
    assert state == "queue-limit-unknown"
    assert "could not be read" in detail and "282900" in detail
    lines = repair_lines(state, rows, None)
    assert any("Workspace keys are rejected by every Admin endpoint" in line
               for line in lines)
    assert any("raw count" in line for line in lines)


def test_the_same_workspace_key_twice_does_not_double_the_depth():
    assert workspace_keys("k1", "k2,k3") == ["k1", "k2", "k3"]
    assert workspace_keys("k1", "k1, k1 ,") == ["k1"]
    assert workspace_keys("", None) == []
    assert workspace_keys(None, "k9") == ["k9"]


def test_the_repair_names_the_biggest_holder_and_the_per_batch_cap():
    rows = queue_rows(BATCHES)
    lines = repair_lines("queue-near-limit", rows, 300000)
    assert any("msgbatch_01Rf alone holds 214900 of the 300000" in line
               for line in lines)
    assert any("100000 requests or 256 MB" in line for line in lines)
    assert any("24 hour window" in line for line in lines)
    assert top_holders(rows, 1)[0]["id"] == "msgbatch_01Rf"
    assert top_holders([], 3) == []
    assert repair_lines("queue-clear", rows, 300000)[0].startswith("nothing to change")
