from file_store_quota_audit import (by_purpose, epoch, file_row,
                                    grade_concentration, grade_expiry,
                                    grade_outliers, grade_total, human,
                                    repair_lines, totals)

NOW = 1_800_000_000
DAY = 86400
GIB = 1024 ** 3


def oai(fid, size, purpose="batch", days_old=1, expires=None):
    return file_row({"id": fid, "bytes": size, "purpose": purpose,
                     "filename": fid + ".jsonl",
                     "created_at": NOW - int(days_old * DAY),
                     "expires_at": expires}, "openai")


def test_the_ceiling_is_an_argument_because_no_endpoint_reports_it():
    used = 90 * GIB
    tight, detail = grade_total(used, 100 * GIB)
    assert tight == "quota-critical"
    assert "90.0%" in detail and "headroom" in detail
    # Same measured total, different documented ceiling, different verdict.
    assert grade_total(used, 1000 * GIB)[0] == "quota-headroom"
    assert grade_total(used, 140 * GIB)[0] == "quota-warning"
    # No denominator at all is an inventory, not a percentage.
    state, detail = grade_total(used, 0)
    assert state == "quota-unknown"
    assert "without a denominator" in detail
    assert any("--quota-bytes" in line for line in repair_lines(state))


def test_two_providers_normalise_to_one_shape_and_one_clock():
    a = file_row({"id": "file-a1", "bytes": 2048, "purpose": "batch_output",
                  "created_at": 1_700_000_000, "expires_at": None}, "openai")
    b = file_row({"id": "file_b2", "size_bytes": 2048, "filename": "doc.pdf",
                  "created_at": "2023-11-14T22:13:20Z",
                  "expires_at": None}, "anthropic")
    assert a["size"] == b["size"] == 2048
    assert a["purpose"] == "batch_output"
    # Anthropic has no purpose concept, so the row says so rather than guessing.
    assert b["purpose"] == "unclassified"
    # One clock: an integer and an RFC 3339 string land on the same second.
    assert a["created_at"] == b["created_at"] == 1_700_000_000
    assert epoch("2023-11-14T22:13:20+00:00") == 1_700_000_000
    assert epoch("2023-11-14T23:13:20+01:00") == 1_700_000_000
    assert epoch(None) == 0 and epoch("") == 0 and epoch("last tuesday") == 0
    assert a["expires_at"] is None and a["expiry_reported"] is True
    # A row with no expires_at key at all: OpenAI omits it rather than nulling.
    assert file_row({"id": "file-c3", "bytes": 1}, "openai")["expiry_reported"] is False
    assert file_row(None, "openai")["size"] == 0
    assert file_row({"bytes": "nonsense"}, "openai")["size"] == 0
    assert human(2048) == "2.0 KiB" and human(0) == "0 B" and human(None) == "0 B"


def test_concentration_only_fires_when_one_class_really_dominates():
    lopsided = [oai("file-1", 90 * GIB, "batch_output"),
                oai("file-2", 5 * GIB, "fine-tune"),
                oai("file-3", 5 * GIB, "user_data")]
    tot = totals(lopsided)
    assert tot == {"count": 3, "bytes": 100 * GIB}
    ranked = by_purpose(lopsided)
    assert ranked[0][0] == "batch_output" and ranked[0][1] == 1
    state, detail = grade_concentration(ranked, tot["bytes"])
    assert state == "purpose-dominates"
    assert "batch_output is 90.0%" in detail
    assert any("DELETE /v1/files/{file_id}" in line for line in repair_lines(state))
    # An evenly spread store has nothing to sweep first.
    even = [oai("file-4", 10 * GIB, "batch"), oai("file-5", 10 * GIB, "fine-tune"),
            oai("file-6", 10 * GIB, "user_data")]
    flat, flat_detail = grade_concentration(by_purpose(even), totals(even)["bytes"])
    assert flat == "purpose-even"
    assert "largest is" in flat_detail
    assert grade_concentration([], 0)[0] == "purpose-even"


def test_the_per_file_cap_is_a_second_ceiling_and_not_a_share_of_the_first():
    rows = [oai("file-9f1", 487_000_000, "fine-tune"), oai("file-a2", 1024)]
    # A fraction of a percent of a huge quota, and still a finding.
    assert grade_total(totals(rows)["bytes"], 16 * 1024 * GIB)[0] == "quota-headroom"
    state, detail, big = grade_outliers(rows, 512_000_000)
    assert state == "file-near-cap"
    assert "1 file(s)" in detail and "80%" in detail
    assert [row["id"] for row in big] == ["file-9f1"]
    assert any("second ceiling" in line for line in repair_lines(state))
    assert grade_outliers(rows, 16 * GIB)[0] == "file-sizes-fine"
    assert grade_outliers(rows, 0)[0] == "cap-unknown"


def test_expiry_is_the_only_grader_that_describes_the_future():
    stale = [oai("file-1", GIB, days_old=200), oai("file-2", GIB, days_old=200),
             oai("file-3", GIB, days_old=2)]
    state, detail = grade_expiry(stale, NOW, 90)
    assert state == "no-expiry-policy"
    assert "3 of 3 file(s) have no expires_at" in detail
    assert "2 of those are older than 90 day(s)" in detail
    assert any("expires_in_seconds" in line for line in repair_lines(state))
    # A store where everything expires has a lifecycle and is not this note.
    covered = [oai("file-4", GIB, expires=NOW + 10 * DAY)]
    clean, clean_detail = grade_expiry(covered, NOW, 90)
    assert clean == "expiry-covered"
    assert "lifecycle" in clean_detail
    assert repair_lines(clean) == []
    assert grade_expiry([], NOW, 90)[0] == "expiry-none"


def test_every_repair_is_printed_and_none_of_them_reclaims_anything():
    for state in ("quota-critical", "quota-warning", "purpose-dominates",
                  "file-near-cap", "no-expiry-policy", "quota-unknown"):
        lines = repair_lines(state)
        assert lines, state
        assert all(isinstance(line, str) and line for line in lines)
    assert any("cannot be recovered" in line
               for line in repair_lines("purpose-dominates"))
    assert repair_lines("quota-headroom") == []
    assert repair_lines("expiry-covered") == []
