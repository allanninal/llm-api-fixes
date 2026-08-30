from batch_output_unclaimed_audit import (anthropic_rows, by_urgency,
                                          counts_by_state, days_left,
                                          file_index, openai_deadline,
                                          openai_rows, parse_time, read_ledger,
                                          repair_lines, verdict)

NOW = 1_800_000_000
DAY = 86400

OPENAI_BATCHES = [
    {"id": "batch_fresh", "status": "completed", "created_at": NOW - 26 * DAY,
     "completed_at": NOW - 26 * DAY, "output_file_id": "file_soon",
     "request_counts": {"total": 88300, "completed": 88300, "failed": 0}},
    {"id": "batch_gone", "status": "completed", "created_at": NOW - 60 * DAY,
     "completed_at": NOW - 60 * DAY, "output_file_id": "file_2b7c",
     "request_counts": {"total": 40000, "completed": 40000, "failed": 0}},
    {"id": "batch_open", "status": "completed", "created_at": NOW - 3 * DAY,
     "completed_at": NOW - 3 * DAY, "output_file_id": "file_room",
     "request_counts": {"total": 90000, "completed": 90000, "failed": 0}},
    {"id": "batch_stuck", "status": "in_progress", "created_at": NOW - 62 * 3600},
]

OPENAI_FILES = [
    {"id": "file_soon", "purpose": "batch_output", "bytes": 10,
     "created_at": NOW - 26 * DAY},
    {"id": "file_room", "purpose": "batch_output", "bytes": 10,
     "created_at": NOW - 3 * DAY},
]

ANTHROPIC_BATCHES = [
    {"id": "msgbatch_arch", "processing_status": "ended",
     "created_at": "2026-01-02T00:00:00Z", "ended_at": "2026-01-02T04:00:00Z",
     "archived_at": "2026-01-31T00:00:00Z", "results_url": None,
     "request_counts": {"processing": 0, "succeeded": 12400, "errored": 0,
                        "canceled": 0, "expired": 0}},
    {"id": "msgbatch_open", "processing_status": "in_progress",
     "created_at": "2026-01-02T00:00:00Z",
     "request_counts": {"processing": 500, "succeeded": 0, "errored": 0,
                        "canceled": 0, "expired": 0}},
]


def test_the_retention_anchors_are_different_on_each_provider():
    index = file_index(OPENAI_FILES)
    # No expires_at on the file, so 30 days from completion.
    deadline, source = openai_deadline(OPENAI_BATCHES[0], index["file_soon"])
    assert source == "completed_at + 30d"
    assert days_left(deadline, NOW) == 4
    # The platform's own expires_at wins whenever it is set.
    stamped = dict(index["file_soon"], expires_at=NOW + 2 * DAY)
    deadline, source = openai_deadline(OPENAI_BATCHES[0], stamped)
    assert source == "expires_at" and days_left(deadline, NOW) == 2
    assert openai_deadline({}, {}) == (None, "unknown")
    assert days_left(None, NOW) is None
    # Anthropic counts 29 days from created_at, not from ended_at.
    created = parse_time("2026-01-02T00:00:00Z")
    rows = anthropic_rows([dict(ANTHROPIC_BATCHES[0], archived_at=None)], set(),
                          created + 27 * DAY, 5)
    assert rows[0]["state"] == "expiring"
    assert "created_at + 29d" in rows[0]["detail"]


def test_a_missing_output_file_is_lost_and_not_merely_unclaimed():
    rows = openai_rows(OPENAI_BATCHES, file_index(OPENAI_FILES), set(), NOW, 5)
    states = {r["id"]: r["state"] for r in rows}
    assert states["batch_gone"] == "lost"
    assert states["batch_fresh"] == "expiring"
    assert states["batch_open"] == "unclaimed"
    lost = [r for r in rows if r["state"] == "lost"][0]
    assert "no longer exists" in lost["detail"]
    # Anthropic says the same thing with archived_at.
    arch = anthropic_rows(ANTHROPIC_BATCHES, set(), NOW, 5)
    assert [r["state"] for r in arch if r["id"] == "msgbatch_arch"] == ["lost"]


def test_never_polled_never_fetched_and_never_claimed_are_one_pass():
    rows = (openai_rows(OPENAI_BATCHES, file_index(OPENAI_FILES), set(), NOW, 5)
            + anthropic_rows(ANTHROPIC_BATCHES, set(), NOW, 5))
    counts = counts_by_state(rows)
    # Created and never polled.
    assert counts["stalled"] == 2
    stalled = [r for r in rows if r["state"] == "stalled"]
    assert any("past the 24 h window" in r["detail"] for r in stalled)
    # Ended and never claimed.
    assert counts["unclaimed"] == 1
    # A batch in the ledger goes quiet, which is the whole point of the join.
    claimed = openai_rows([OPENAI_BATCHES[2]], file_index(OPENAI_FILES),
                          {"batch_open"}, NOW, 5)
    assert claimed[0]["state"] == "claimed"
    assert "in the ingest ledger" in claimed[0]["detail"]


def test_the_verdict_leads_with_what_you_can_still_act_on():
    rows = (openai_rows(OPENAI_BATCHES, file_index(OPENAI_FILES), set(), NOW, 5)
            + anthropic_rows(ANTHROPIC_BATCHES, set(), NOW, 5))
    state, detail = verdict(rows, {"x"}, 5)
    assert state == "batch-output-expiring"
    assert "expire within 5 days" in detail
    assert "already unrecoverable" in detail and "never claimed" in detail
    # Order on the page follows the same rule, soonest deadline first.
    ordered = [r["state"] for r in by_urgency(rows)]
    assert ordered[0] == "expiring"
    assert ordered.index("lost") < ordered.index("unclaimed")
    assert ordered.index("unclaimed") < ordered.index("stalled")
    # Without anything expiring, the lost pile leads.
    no_expiring = [r for r in rows if r["state"] != "expiring"]
    assert verdict(no_expiring, {"x"}, 5)[0] == "batch-output-lost"
    assert verdict([], set(), 5)[0] == "batch-output-clean"


def test_an_absent_ledger_is_reported_rather_than_assumed_away():
    rows = openai_rows([OPENAI_BATCHES[2]], file_index(OPENAI_FILES), set(), NOW, 5)
    state, detail = verdict(rows, set(), 5)
    assert state == "batch-output-unclaimed"
    assert "no ingest ledger was supplied" in detail
    lines = repair_lines(state, rows, set())
    assert any("neither API offers a read receipt" in line for line in lines)
    assert read_ledger("# note\nbatch_a\nbatch_b,batch_a\n") == {"batch_a",
                                                                  "batch_b"}
    assert read_ledger("") == set()


def test_the_repair_hands_the_other_half_to_the_error_file_note():
    rows = (openai_rows(OPENAI_BATCHES, file_index(OPENAI_FILES), set(), NOW, 5)
            + anthropic_rows(ANTHROPIC_BATCHES, set(), NOW, 5))
    lines = repair_lines("batch-output-expiring", rows, {"x"})
    assert any("error_file_id, the list of rows that failed" in line
               for line in lines)
    assert any("download the expiring outputs today" in line for line in lines)
    assert any("re-run and re-paid" in line for line in lines)
    assert any("stale object rather than" in line for line in lines)
    clean = repair_lines("batch-output-clean", [], {"x"})
    assert clean[0].startswith("nothing outstanding")
