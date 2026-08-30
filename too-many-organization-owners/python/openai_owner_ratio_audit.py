"""Find an OpenAI organization where the owner role is the default.

Read only. Paged GETs against /v1/organization/users, /admin_api_keys,
/projects and each project's /users, with an organization admin key. Every
request is a GET and no request body is constructed.

No key value is read or printed. The admin key listing is used for its `owner`
block only, and email addresses are masked, because this report is a list of
named colleagues.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_owner_ratio_audit")

API = "https://api.openai.com/v1"
DAY = 86400

OWNER = "owner"
READER = "reader"
OTHER = "other"

FINDINGS = ("everyone-is-owner", "owner-majority", "owner-count-high")


def humans(users):
    """The roster with service accounts removed. Pure.

    Service accounts are returned by this endpoint alongside people and are
    frequently owners because that is what the job needed. Counting them
    inflates the ratio and produces a report that recommends demoting a cron.
    """
    return [u for u in (users or []) if not (u or {}).get("is_service_account")]


def role_of(user):
    """Normalise one member's org role. Pure.

    An unrecognised role is filed under "other" rather than folded into reader:
    a future role this script has never heard of should show up in the output
    as unknown, not be silently counted as restricted.
    """
    raw = str((user or {}).get("role") or "").strip().lower()
    return raw if raw in (OWNER, READER) else OTHER


def role_counts(people):
    """{role: count} over a roster. Pure."""
    counts = {OWNER: 0, READER: 0, OTHER: 0}
    for person in people or []:
        counts[role_of(person)] += 1
    return counts


def owner_ratio(counts):
    """Owners as a share of the roster. Pure. 0.0 when the roster is empty."""
    data = counts or {}
    total = sum(int(data.get(r) or 0) for r in (OWNER, READER, OTHER))
    if total <= 0:
        return 0.0
    return int(data.get(OWNER) or 0) / float(total)


def mask(email):
    """Hide the local part of an email address. Pure. Non-emails pass through.

    Every row of this report names a colleague and their privileges. Masking by
    default costs nothing and makes the output safe to paste into a channel.
    """
    text = str(email or "").strip()
    if "@" not in text:
        return text or "unknown"
    local, _, domain = text.partition("@")
    if not local:
        return text
    return local[0] + "***@" + domain


def unused_privilege(user, now, days=180):
    """Has this member authenticated an API request recently? Pure.

    Reported as a question, never as a verdict. A null api_key_last_used_at
    means no API request, which is exactly what an administrator who works
    through the console looks like.
    """
    stamp = (user or {}).get("api_key_last_used_at")
    if not stamp:
        return (True, "no API key use on record")
    try:
        age = (int(now) - int(stamp)) // DAY
    except (TypeError, ValueError):
        return (True, "unreadable api_key_last_used_at")
    if age >= days:
        return (True, "last key use %d day(s) ago" % age)
    return (False, "last key use %d day(s) ago" % age)


def admin_key_owners(keys):
    """{owner_id: owner_name} from the admin key listing. Pure.

    Reads the owner block and nothing else. The key value is not returned by
    this endpoint and is not wanted.
    """
    out = {}
    for key in keys or []:
        owner = (key or {}).get("owner") or {}
        oid = owner.get("id") or (owner.get("user") or {}).get("id")
        if not oid:
            continue
        name = owner.get("name") or (owner.get("user") or {}).get("email") or "unnamed"
        out[str(oid)] = str(name)
    return out


def project_owner_share(members):
    """(owners, total, ratio) for one project's member list. Pure."""
    rows = [m for m in (members or []) if not (m or {}).get("is_service_account")]
    owners = sum(1 for m in rows
                 if str((m or {}).get("role") or "").strip().lower() == OWNER)
    total = len(rows)
    return (owners, total, (owners / float(total)) if total else 0.0)


def verdict(counts, min_members=3, ratio_max=0.50, count_max=5):
    """Classify the roster. Pure. Returns (state, detail).

    The member floor comes first. Two owners in a three-person company is a
    company, not a governance finding, and grading it produces a report that
    nobody can act on and everybody learns to ignore.
    """
    data = counts or {}
    owners = int(data.get(OWNER) or 0)
    total = sum(int(data.get(r) or 0) for r in (OWNER, READER, OTHER))
    ratio = owner_ratio(data)

    if total < min_members:
        return ("too-few-members",
                "%d human member(s) in the organization, too few for a role "
                "distribution to mean anything" % total)
    if ratio >= 0.90 and owners >= 3:
        return ("everyone-is-owner",
                "%d of %d human member(s) hold the owner role (%.0f%%). The "
                "distinction between owner and reader has stopped existing here."
                % (owners, total, ratio * 100))
    if ratio > ratio_max:
        return ("owner-majority",
                "%d of %d human member(s) hold the owner role (%.0f%%)"
                % (owners, total, ratio * 100))
    if owners > count_max:
        return ("owner-count-high",
                "%d of %d human member(s) hold the owner role. The share is "
                "fine and the absolute count is past the %d this audit treats "
                "as a working ceiling, which is a convention rather than a "
                "platform rule." % (owners, total, count_max))
    return ("scoped",
            "%d of %d human member(s) hold the owner role (%.0f%%)"
            % (owners, total, ratio * 100))


def repair_lines(state, scim_owners=0, key_holders=0, loose_projects=0):
    """The repair for one roster verdict. Pure. Printed, never performed."""
    if state not in FINDINGS:
        return []
    lines = [
        "demote to reader anyone who does not administer billing, keys or "
        "projects, with POST /v1/organization/users/{user_id} and role reader.",
        "grant a project role instead, so people keep the access they actually "
        "use: POST /v1/organization/projects/{project_id}/users with member.",
    ]
    if scim_owners:
        lines.append("%d owner(s) are SCIM-managed. Change the group mapping in "
                     "the identity provider; a role changed through this API is "
                     "reverted at the next sync." % scim_owners)
    if key_holders:
        lines.append("%d owner(s) hold an admin API key. Revoke the key before "
                     "the role, or the credential outlives the demotion."
                     % key_holders)
    if loose_projects:
        lines.append("%d project(s) also grant owner to every member, so an "
                     "org-level demotion alone will not change what anybody can "
                     "do there." % loose_projects)
    return lines


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an "
                         "organization admin key, not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def paged(session, path, **params):
    """Walk an after/last_id cursor listing."""
    params = dict(params)
    while True:
        page = get(session, path, **params)
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params["after"] = page.get("last_id") or (data[-1] or {}).get("id")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=180,
                    help="days of API silence before privilege is questioned")
    ap.add_argument("--ratio", type=float, default=0.50,
                    help="owner share above which the roster is flagged")
    ap.add_argument("--max-owners", type=int, default=5,
                    help="absolute owner count treated as a working ceiling")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key; a "
                  "project key cannot read /v1/organization/*")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})
    now = int(time.time())

    users = list(paged(s, "/organization/users", limit=100))
    people = humans(users)
    counts = role_counts(people)
    owners = [p for p in people if role_of(p) == OWNER]
    scim = [p for p in owners if p.get("is_scim_managed")]
    holders = admin_key_owners(paged(s, "/organization/admin_api_keys", limit=100))

    loose = 0
    projects = [p for p in paged(s, "/organization/projects", limit=100)
                if str(p.get("status") or "").lower() != "archived"]
    for project in projects:
        members = list(paged(s, "/organization/projects/%s/users" % project.get("id"),
                             limit=100))
        got, total, ratio = project_owner_share(members)
        if total and ratio >= 0.90:
            loose += 1

    log.info("%d member(s), %d service account(s) excluded, %d SCIM-managed",
             len(people), len(users) - len(people), len(scim))

    state, detail = verdict(counts, ratio_max=args.ratio, count_max=args.max_owners)
    if state not in FINDINGS:
        log.info("%-18s %s", state, detail)
        return 0

    log.warning("%-18s %s", state, detail)
    for person in sorted(owners, key=lambda p: int(p.get("added_at") or 0)):
        _, note = unused_privilege(person, now, args.days)
        extra = " holds an admin API key" if str(person.get("id")) in holders else ""
        log.warning("  %-24s owner   added %s  %s%s", mask(person.get("email")),
                    time.strftime("%Y-%m-%d",
                                  time.gmtime(int(person.get("added_at") or 0))),
                    note, extra)
    if loose:
        log.warning("  project roles: %d of %d project(s) also grant owner to "
                    "every member", loose, len(projects))
    key_holders = sum(1 for p in owners if str(p.get("id")) in holders)
    for line in repair_lines(state, len(scim), key_holders, loose):
        log.warning("  repair: %s", line)
    log.info("1 finding(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
