from openai_orphaned_key_audit import owner_label, verdict

NOW = 1_756_000_000  # a fixed clock, so these never age out


def make(**over):
    key = {
        "id": "key_abc",
        "redacted_value": "sk-proj-...aB3d",
        "owner_project_access": "active",
        "last_used_at": NOW - 3600,
        "owner": {"type": "user", "user": {"email": "dev@example.com"}},
    }
    key.update(over)
    return key


def test_active_owner_is_not_a_finding():
    state, _ = verdict(make(), NOW)
    assert state == "in-force"


def test_inactive_owner_used_today_is_production_traffic():
    state, detail = verdict(make(owner_project_access="inactive"), NOW)
    assert state == "serving"
    assert "re-issue" in detail


def test_inactive_owner_long_idle_is_orphaned_not_serving():
    state, detail = verdict(
        make(owner_project_access="inactive", last_used_at=NOW - 90 * 86400), NOW)
    assert state == "orphaned"
    assert "90 day(s)" in detail


def test_inactive_owner_never_used_is_the_safe_one():
    state, detail = verdict(
        make(owner_project_access="inactive", last_used_at=None), NOW)
    assert state == "dormant"
    assert "revoke first" in detail


def test_missing_access_field_is_never_read_as_active():
    # The whole point: an absent field is an unanswered question, not a clean org.
    key = make()
    del key["owner_project_access"]
    state, detail = verdict(key, NOW)
    assert state == "unknown"
    assert "owner_project_access=any" in detail


def test_unrecognised_access_value_is_not_silently_fine():
    assert verdict(make(owner_project_access="pending"), NOW)[0] == "unknown"


def test_a_service_account_key_is_judged_on_the_same_field():
    key = make(owner_project_access="inactive",
               owner={"type": "service_account",
                      "service_account": {"name": "batch-runner"}})
    assert verdict(key, NOW)[0] == "serving"
    assert owner_label(key) == "batch-runner"


def test_owner_label_prefers_the_email():
    assert owner_label(make()) == "dev@example.com"
    assert owner_label({"owner": {"type": "user"}}) == "user"
    assert owner_label({}) == "unknown owner"


def test_the_hot_window_is_a_parameter_not_a_constant():
    key = make(owner_project_access="inactive", last_used_at=NOW - 20 * 86400)
    assert verdict(key, NOW, hot_days=7)[0] == "orphaned"
    assert verdict(key, NOW, hot_days=30)[0] == "serving"
