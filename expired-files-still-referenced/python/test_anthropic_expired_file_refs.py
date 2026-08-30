from anthropic_expired_file_refs import (ID_BATCH, chunks, classify_id, epoch,
                                        file_row, human, missing_ids, parse_ids,
                                        repair_lines)

NOW = 1_800_000_000
DAY = 86400


def row(fid, expires_in_days=None, has_field=True, size=2048):
    body = {"id": fid, "type": "file", "filename": fid + ".pdf",
            "size_bytes": size, "created_at": "2026-01-01T00:00:00Z",
            "downloadable": False}
    if has_field:
        body["expires_at"] = (None if expires_in_days is None else
                              _stamp(NOW + int(expires_in_days * DAY)))
    return file_row(body)


def _stamp(when):
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(when))


def test_an_expired_file_still_answers_and_fails_every_use():
    state, detail = classify_id(row("file_011a", -11.3), NOW, 7.0)
    assert state == "expired"
    assert "expired 11.3 day(s) ago" in detail
    assert "the metadata still answers" in detail
    assert "every actual use of this id fails" in detail
    lines = repair_lines(state)
    assert any("cannot be restored" in line for line in lines)
    assert any("DELETE /v1/files/{file_id}" in line for line in lines)
    assert any("30 day window" in line for line in lines)


def test_an_expiry_cannot_be_extended_so_the_repair_never_suggests_it():
    state, detail = classify_id(row("file_02b7", 4.1), NOW, 7.0)
    assert state == "expiring"
    assert "expires in 4.1 day(s)" in detail
    assert "cannot be extended" in detail
    lines = repair_lines(state)
    assert any("set once at upload" in line for line in lines)
    assert any("Re-upload before the date" in line for line in lines)
    assert not any("extend the" in line for line in lines)
    # Outside the window it is simply live, with the runway printed.
    live, live_detail = classify_id(row("file_05e4", 61.8), NOW, 7.0)
    assert live == "live" and "61.8 day(s)" in live_detail
    assert repair_lines(live) == []


def test_an_id_the_api_declines_to_return_is_the_strongest_signal():
    state, detail = classify_id(None, NOW, 7.0)
    assert state == "gone"
    assert "not returned by the ids lookup" in detail
    assert "30 day metadata window" in detail
    assert any("no read will recover" in line for line in repair_lines(state))
    # Nothing is raised for an unresolvable id, so the diff is the only signal.
    asked = ["file_01", "file_02", "file_03"]
    assert missing_ids(asked, ["file_02"]) == ["file_01", "file_03"]
    assert missing_ids(asked, asked) == []
    assert missing_ids([], ["file_09"]) == []


def test_a_missing_expiry_field_disables_the_check_rather_than_passing_it():
    blind = row("file_06f1", has_field=False)
    assert blind["expiry_reported"] is False
    state, detail = classify_id(blind, NOW, 7.0)
    assert state == "expiry-not-reported"
    assert "could not run" in detail
    assert any("files-api-2025-04-14" in line for line in repair_lines(state))
    # A null expiry is a different fact and must not be confused with it.
    perm = row("file_04d2", None)
    assert perm["expiry_reported"] is True and perm["expires_at"] is None
    assert classify_id(perm, NOW, 7.0)[0] == "no-expiry"
    assert any("never leaves the storage total" in line
               for line in repair_lines("no-expiry"))


def test_batching_is_a_contract_and_not_a_performance_setting():
    ids = ["file_%03d" % n for n in range(250)]
    batched = chunks(ids)
    assert [len(b) for b in batched] == [100, 100, 50]
    assert all(len(b) <= ID_BATCH for b in batched)
    # Asking for a bigger batch does not get one: 100 is documented, not tuned.
    assert [len(b) for b in chunks(ids, 500)] == [100, 100, 50]
    # De-duplication happens before the split, as the documentation specifies.
    assert chunks(["a", "a", " a ", "b", ""]) == [["a", "b"]]
    assert chunks([]) == [] and chunks(None) == []


def test_the_dates_and_the_export_survive_what_is_really_in_them():
    ids = parse_ids("file_01\n\n# exported 2026-08-31\nfile_02  # oldest\n"
                    "file_01\n   \nfile_03\n")
    assert ids == ["file_01", "file_02", "file_03"]
    assert parse_ids("") == [] and parse_ids(None) == []
    assert epoch("2023-11-14T22:13:20Z") == 1_700_000_000
    assert epoch("2023-11-14T22:13:20.512Z") == 1_700_000_000
    assert epoch("2023-11-14T23:13:20+01:00") == 1_700_000_000
    assert epoch(None) == 0 and epoch("soon") == 0 and epoch(True) == 0
    junk = file_row({"id": "file_07", "size_bytes": "big", "expires_at": "soon"})
    assert junk["size"] == 0
    # Unparseable is not permanent, but it does read as no usable date, so the
    # verdict is no-expiry rather than a fabricated one.
    assert junk["expires_at"] is None and junk["expiry_reported"] is True
    assert file_row(None)["id"] == "" and human(2048) == "2.0 KiB"
