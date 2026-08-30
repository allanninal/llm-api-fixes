"""Find service account keys that have never been rotated.

Read only. Four GETs against the OpenAI Administration API with an admin key:
projects, service accounts, project keys and the audit log. Nothing is created,
changed or removed, and no key value is printed.

The clock is created_at on the newest key belonging to each service account.
There is no rotated_at field anywhere on either provider, so age since minting
is the only evidence available, and the key count decides which of three
findings it is.

The audit log corroborates an absence at the PROJECT level only: the
api_key.created event carries project.id and actor and does not name a service
account. An empty or unreachable audit log is reported as unavailable rather
than as agreement, because audit logging is gated to organizations that have it
enabled and silence from it means nothing either way.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_key_rotation_clock")

API = "https://api.openai.com/v1"

SINGLE_STALE = "single-stale-key"
STALE = "stale-key"
UNFINISHED = "unfinished-rotation"
NO_KEYS = "service-account-with-no-keys"
ROTATING = "rotating"
TOO_NEW = "too-new"
FINDINGS = (SINGLE_STALE, STALE, UNFINISHED, NO_KEYS)

AUDIT_CONFIRMED = "confirmed-at-project-level"
AUDIT_ACTIVITY = "creation-activity-in-window"
AUDIT_UNAVAILABLE = "audit-unavailable"


def age_days(stamp, now):
    """Whole days between a unix timestamp and now. Pure. None when unreadable."""
    if stamp is None or stamp == "" or isinstance(stamp, bool):
        return None
    try:
        when = dt.datetime.fromtimestamp(float(stamp), dt.timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return int((now - when).total_seconds() // 86400)


def service_account_id(key):
    """The service account a key belongs to, or None. Pure."""
    owner = (key or {}).get("owner")
    if not isinstance(owner, dict):
        return None
    if str(owner.get("type") or "").strip().lower() != "service_account":
        return None
    account = owner.get("service_account")
    if not isinstance(account, dict):
        return None
    return str(account.get("id") or "") or None


def group_by_account(keys):
    """Group service-account keys by owner.service_account.id. Pure.

    Keys owned by a user are dropped here rather than counted as unattributed,
    because a personal key in a project is a finding for a different note and
    counting it towards a service account's key total would make a single-key
    account look like it has an overlap window it does not have.
    """
    out = {}
    for key in keys or []:
        account = service_account_id(key)
        if account:
            out.setdefault(account, []).append(key)
    return out


def newest_and_oldest(keys, now):
    """(newest_age, oldest_age) in days across a key group. Pure.

    The newest is the rotation clock. Using the oldest instead reports a
    service account that rotated last week as stale, because the key it
    replaced is still in the list until somebody revokes it.
    """
    ages = [a for a in (age_days((k or {}).get("created_at"), now) for k in keys or [])
            if a is not None]
    if not ages:
        return (None, None)
    return (min(ages), max(ages))


def rotation_verdict(account, keys, now, stale_after=180, min_age=30):
    """Classify one service account's rotation state. Pure. (state, detail).

    The key count is an input rather than a detail, because one stale key and
    two stale keys are different problems. With one key there has never been a
    moment when two credentials were valid at once, so every rotation is a
    synchronised cutover with no rollback, and that is the reason it keeps
    being deferred.
    """
    name = str((account or {}).get("name") or (account or {}).get("id") or "(unnamed)")
    rows = list(keys or [])
    if not rows:
        created = age_days((account or {}).get("created_at"), now)
        return (NO_KEYS,
                "service account %s has no keys at all%s"
                % (name, "" if created is None else
                   ", and was created %d day(s) ago" % created))

    newest, oldest = newest_and_oldest(rows, now)
    if newest is None:
        return (TOO_NEW, "no readable created_at on any of its %d key(s)" % len(rows))
    if newest < min_age and len(rows) == 1:
        return (TOO_NEW, "its only key is %d day(s) old" % newest)
    if newest < stale_after:
        if oldest >= stale_after and len(rows) > 1:
            return (UNFINISHED,
                    "newest key %d day(s) old, oldest %d day(s) and still live"
                    % (newest, oldest))
        return (ROTATING, "newest key %d day(s) old" % newest)
    if len(rows) == 1:
        return (SINGLE_STALE,
                "newest key %d day(s) old, and it is the only one" % newest)
    return (STALE,
            "newest key %d day(s) old across %d key(s)" % (newest, len(rows)))


def corroboration(events, project_id, audit_reachable=True, days=180):
    """What the audit log can and cannot confirm. Pure. (state, detail).

    Three outcomes and only one of them is corroboration. The api_key.created
    event names a project and an actor and never a service account, so the
    strongest available statement is about the project. An empty or unreachable
    log is reported as unavailable, because audit logging is gated and its
    silence is not evidence.
    """
    if not audit_reachable:
        return (AUDIT_UNAVAILABLE,
                "the audit log could not be read, so nothing here is "
                "corroborated. Audit logging is gated to organizations that "
                "have it enabled and its silence is not evidence.")
    rows = list(events or [])
    if not rows:
        return (AUDIT_UNAVAILABLE,
                "the audit log returned no events of any kind in %d day(s), "
                "which can mean nothing was minted or can mean nothing is "
                "being recorded. Treated as unavailable rather than clean."
                % days)
    here = [e for e in rows
            if str(((e or {}).get("project") or {}).get("id") or "") == str(project_id)]
    if here:
        return (AUDIT_ACTIVITY,
                "%d api_key.created event(s) in this project in %d day(s), so "
                "something was minted here. The event does not name a service "
                "account, so it neither confirms nor clears any one of them."
                % (len(here), days))
    return (AUDIT_CONFIRMED,
            "no api_key.created events in this project in %d day(s). That is a "
            "project-level fact: the event carries project.id and actor and "
            "not the service account, so the per-account age above remains the "
            "evidence for any single account." % days)


def rotation_plan(project_id, account_name, single_key):
    """The overlap rotation, printed and never performed. Pure."""
    steps = []
    if single_key:
        steps.append("mint a second key first. One key means every rotation is "
                     "a hard cutover with no rollback, which is the actual "
                     "reason this has not happened yet.")
    steps.extend([
        "mint the replacement with an admin POST to /v1/organization/projects/"
        "%s/service_accounts/{service_account_id}/api_keys for %s. The value "
        "is returned exactly once." % (project_id, account_name),
        "deploy the new value everywhere the old one is held, then watch the "
        "old key: its last_used_at should stop advancing within one traffic "
        "cycle. Do not skip this; it is the only rollback you get.",
        "revoke the old key with a DELETE on /v1/organization/projects/%s/"
        "api_keys/{api_key_id}, and diary the next rotation at 90 days. "
        "Project keys have no expires_at, so nothing will remind you."
        % project_id,
    ])
    return steps


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an admin "
                         "key (sk-admin-), not a project key" % r.status_code)
    r.raise_for_status()
    return r.json()


def paged(session, path, params, limit_pages=20):
    """Walk an administration listing on has_more / last_id."""
    params = dict(params)
    for _ in range(limit_pages):
        page = get(session, path, params)
        yield page
        if not page.get("has_more") or not page.get("last_id"):
            return
        params["after"] = page["last_id"]


def collect(session, path, params):
    rows = []
    for page in paged(session, path, params):
        rows.extend(page.get("data") or [])
    return rows


def read_audit_log(session, days):
    """Read api_key.created events, tolerating an organization without them.

    Returns (events, reachable). A 4xx here is not fatal: audit logging is not
    enabled everywhere, and a script that dies on it would report nothing about
    the key ages it already has in hand.
    """
    since = int((dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=days)).timestamp())
    try:
        return (collect(session, "/organization/audit_logs",
                        {"limit": 100, "event_types[]": ["api_key.created"],
                         "effective_at[gte]": since}), True)
    except requests.HTTPError as err:
        status = getattr(getattr(err, "response", None), "status_code", None)
        log.warning("audit log unreadable (%s): rotation ages below stand on "
                    "created_at alone", status)
        return ([], False)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stale-after", type=int, default=180,
                    help="days since the newest key was minted (default 180)")
    ap.add_argument("--min-age", type=int, default=30,
                    help="days before a new service account is graded at all")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an admin key (sk-admin-) with read "
                  "scopes; a project key cannot read /v1/organization/*")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})
    now = dt.datetime.now(dt.timezone.utc)

    events, reachable = read_audit_log(s, args.stale_after)
    if reachable:
        projects_seen = {str(((e or {}).get("project") or {}).get("id") or "")
                         for e in events}
        log.info("audit log: %d api_key.created event(s) in %d day(s) across "
                 "%d project(s)", len(events), args.stale_after,
                 len([p for p in projects_seen if p]))

    projects = collect(s, "/organization/projects",
                       {"limit": 100, "include_archived": "true"})

    accounts_seen = 0
    keys_seen = 0
    findings = 0

    for project in projects:
        pid = project.get("id")
        if not pid:
            continue
        name = project.get("name") or pid
        accounts = collect(s, "/organization/projects/%s/service_accounts" % pid,
                           {"limit": 100})
        keys = collect(s, "/organization/projects/%s/api_keys" % pid,
                       {"limit": 100, "owner_project_access": "any"})
        grouped = group_by_account(keys)
        accounts_seen += len(accounts)
        keys_seen += sum(len(v) for v in grouped.values())

        project_findings = 0
        for account in accounts:
            rows = grouped.get(str(account.get("id") or ""), [])
            state, detail = rotation_verdict(account, rows, now,
                                             args.stale_after, args.min_age)
            if state not in FINDINGS:
                continue
            findings += 1
            project_findings += 1
            log.warning("%-19s %-11s %-15s %s", state, name,
                        account.get("name") or account.get("id") or "(unnamed)",
                        detail)
            if state in (SINGLE_STALE, STALE, UNFINISHED):
                for step in rotation_plan(pid,
                                          account.get("name") or "(unnamed)",
                                          state == SINGLE_STALE):
                    log.warning("  repair: %s", step)

        if project_findings:
            audit_state, audit_detail = corroboration(events, pid, reachable,
                                                      args.stale_after)
            log.info("corroboration for %s: %s: %s", name, audit_state,
                     audit_detail)

    log.info("%d project(s), %d service account(s), %d service-account key(s)",
             len(projects), accounts_seen, keys_seen)
    log.info("%d finding(s)", findings)
    log.info("there is no rotated_at field on either provider: created_at on "
             "the newest key is the only clock available")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
