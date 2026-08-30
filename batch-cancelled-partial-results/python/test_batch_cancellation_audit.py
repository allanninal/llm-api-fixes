from batch_cancellation_audit import (anthropic_cancel_rows, openai_cancel_rows,
                                      parse_time, repair_lines, salvage_rows,
                                      salvaged_total, stuck_rows, verdict)

NOW = 1_800_000_000

OPENAI = [
    {"id": "batch_c1", "status": "cancelled",
     "request_counts": {"total": 90000, "completed": 61204, "failed": 0},
     "output_file_id": "file_7ac1", "cancelling_at": NOW - 7200,
     "cancelled_at": NOW - 6900},
    {"id": "batch_c2", "status": "cancelling",
     "request_counts": {"total": 400, "completed": 0, "failed": 0},
     "cancelling_at": NOW - 68 * 60},
    {"id": "batch_ok", "status": "completed",
     "request_counts": {"total": 10, "completed": 10, "failed": 0}},
]

ANTHROPIC = [
    {"id": "msgbatch_01Hq", "processing_status": "ended",
     "cancel_initiated_at": "2026-08-20T18:37:24.100435Z",
     "request_counts": {"processing": 0, "succeeded": 41880, "errored": 0,
                        "canceled": 12120, "expired": 0},
     "results_url": "https://api.anthropic.com/v1/messages/batches/x/results"},
    {"id": "msgbatch_02Zz", "processing_status": "in_progress",
     "cancel_initiated_at": None,
     "request_counts": {"processing": 500, "succeeded": 0, "errored": 0,
                        "canceled": 0, "expired": 0}},
]


def test_two_providers_normalise_to_one_row_shape():
    rows = openai_cancel_rows(OPENAI) + anthropic_cancel_rows(ANTHROPIC)
    assert [r["id"] for r in rows] == ["batch_c1", "batch_c2", "msgbatch_01Hq"]
    first = rows[0]
    assert first["done"] == 61204 and first["stopped"] == 28796
    assert first["total"] == 90000 and first["artifact"] == "file_7ac1"
    last = rows[2]
    # succeeded/canceled on Anthropic, and the counts sum to the total.
    assert last["done"] == 41880 and last["stopped"] == 12120
    assert last["total"] == 54000
    assert salvaged_total(rows) == 61204 + 41880
    # A batch with no cancellation initiated is never a row here.
    assert all(r["id"] != "msgbatch_02Zz" for r in rows)


def test_the_timestamp_parser_takes_both_providers_and_refuses_rubbish():
    assert parse_time(NOW) == NOW
    assert parse_time("2026-08-20T18:37:24Z") == 1787251044
    assert parse_time("2026-08-20T18:37:24.100435Z") == 1787251044
    assert parse_time("2026-08-20T18:37:24+00:00") == 1787251044
    for junk in (None, "", "yesterday", True, {}):
        assert parse_time(junk) is None


def test_a_stuck_cancel_is_measured_against_an_argument_not_a_clock():
    rows = openai_cancel_rows(OPENAI)
    stuck = stuck_rows(rows, NOW, 15 * 60)
    assert [r["id"] for r in stuck] == ["batch_c2"]
    # Generous threshold: the same batch is not stuck against a two hour one.
    assert stuck_rows(rows, NOW, 3 * 3600) == []
    # A missing cancelling_at on an in-flight cancel counts as stuck, because
    # "we cannot tell how long" is not the same as "it is fine".
    unknown = [{"id": "batch_x", "in_flight": True, "cancel_started": None,
                "done": 0}]
    assert stuck_rows(unknown, NOW, 15 * 60) == unknown
    # A terminal cancellation is never stuck however old it is.
    assert stuck_rows([rows[0]], NOW, 1) == []


def test_an_unlanded_cancel_outranks_a_salvageable_one():
    rows = openai_cancel_rows(OPENAI) + anthropic_cancel_rows(ANTHROPIC)
    stuck = stuck_rows(rows, NOW, 15 * 60)
    salvage = salvage_rows(rows)
    state, detail = verdict(rows, stuck, salvage)
    assert state == "cancel-stuck"
    assert "mid cancel" in detail and "103084 finished rows" in detail
    # Without the stuck one it drops to the salvage verdict.
    state2, detail2 = verdict(rows, [], salvage)
    assert state2 == "cancel-partial-unclaimed"
    assert "pay for again" in detail2


def test_a_cancel_that_landed_before_anything_ran_is_not_a_finding():
    early = [{"id": "batch_z", "provider": "openai", "status": "cancelled",
              "in_flight": False, "done": 0, "stopped": 400, "total": 400,
              "artifact": None, "cancel_started": NOW - 86400}]
    assert salvage_rows(early) == []
    state, detail = verdict(early, [], [])
    assert state == "cancel-clean"
    assert "nothing to salvage" in detail
    assert repair_lines(state, early)[0].startswith("nothing to collect")
    assert verdict([], [], []) == ("no-cancels",
                                  "no batch on the providers checked has had a "
                                  "cancellation initiated")
    assert repair_lines("no-cancels", []) == []


def test_the_repair_states_the_documented_billing_rule_and_only_that():
    rows = openai_cancel_rows(OPENAI) + anthropic_cancel_rows(ANTHROPIC)
    lines = repair_lines("cancel-partial-unclaimed", rows)
    assert any("custom_id is the only join key" in line for line in lines)
    assert any("canceled and expired requests are not billed" in line
               for line in lines)
    assert any("not documented" in line and "floor" in line for line in lines)
    # Anthropic only: no claim is made about OpenAI billing.
    only_anthropic = anthropic_cancel_rows(ANTHROPIC)
    lines2 = repair_lines("cancel-partial-unclaimed", only_anthropic)
    assert not any("floor" in line for line in lines2)
    assert any("cancelling or canceling has not stopped" in line
               for line in repair_lines("cancel-stuck", rows))
