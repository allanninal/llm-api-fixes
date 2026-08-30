from openai_archived_project_keys import covers_archived, verdict

NOW = 1_756_000_000
ARCHIVED_AT = NOW - 120 * 86400


def project(**over):
    p = {"id": "proj_x", "name": "prototype", "status": "archived",
         "archived_at": ARCHIVED_AT}
    p.update(over)
    return p


def key(last_used_at=None, **over):
    k = {"id": "key_1", "redacted_value": "sk-proj-...9f2c",
         "last_used_at": last_used_at}
    k.update(over)
    return k


def test_a_listing_without_the_parameter_does_not_cover_archived():
    assert covers_archived({"limit": 100}) is False


def test_the_string_false_is_not_truthy_here():
    # The quiet version of this bug: a non-empty string read as "yes".
    assert covers_archived({"include_archived": "false"}) is False
    assert covers_archived({"include_archived": False}) is False


def test_the_parameter_is_recognised_in_the_spellings_that_reach_the_api():
    assert covers_archived({"include_archived": "true"}) is True
    assert covers_archived({"include_archived": "TRUE"}) is True
    assert covers_archived({"include_archived": True}) is True
    assert covers_archived({"include_archived": "1"}) is True


def test_an_active_project_is_out_of_scope():
    state, _ = verdict(project(status="active", archived_at=None), [key(NOW)], NOW)
    assert state == "active"


def test_an_archived_project_with_no_keys_is_clean():
    assert verdict(project(), [], NOW)[0] == "clean"


def test_a_key_used_after_the_archive_is_the_urgent_case():
    state, detail = verdict(project(), [key(ARCHIVED_AT + 10 * 86400)], NOW)
    assert state == "still-serving"
    assert "closed on paper" in detail


def test_a_key_last_used_before_the_archive_is_dead_weight():
    state, detail = verdict(project(), [key(ARCHIVED_AT - 5 * 86400)], NOW)
    assert state == "live-keys"
    assert "since the archive" in detail


def test_a_never_used_key_is_still_reported():
    state, detail = verdict(project(), [key(None)], NOW)
    assert state == "dormant-keys"
    assert "has ever authenticated" in detail


def test_status_archived_without_a_timestamp_is_still_archived():
    # Nothing to compare last_used_at against, so it cannot be still-serving,
    # but it must not fall through to "active" either.
    state, _ = verdict(project(archived_at=None), [key(NOW - 86400)], NOW)
    assert state == "live-keys"
