"""Find OpenAI organization invites that lapsed without anybody noticing.

Read only. Two paged GETs against /v1/organization/invites and
/v1/organization/users with an organization admin key. Every request is a GET
and no request body is constructed.

The detection is a timestamp comparison rather than a status filter: an invite
can read status "pending" while its expires_at is already in the past, and a
filter on status alone never returns that row.

Nothing secret is printed. The invite object carries no token, and the output
is ids, masked email addresses, roles and dates.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_stale_invite_audit")

API = "https://api.openai.com/v1"
DAY = 86400

OWNER = "owner"

FINDINGS = ("expired-but-still-pending", "already-a-member", "pending-stale",
            "expired-uncollected")

# Findings in the order a human should read them. An unclaimed grant of
# organization control outranks a tidy-up, whatever the timestamps say.
SEVERITY = {"expired-but-still-pending": 0, "pending-stale": 1,
            "expired-uncollected": 2, "already-a-member": 3}


def sent_at(invite):
    """When the invite was sent, as unix seconds. Pure. None when unreadable.

    The field goes by two names depending on where you read the reference, so
    both are accepted rather than picking a side and reporting every invite as
    having been sent at the epoch.
    """
    row = invite or {}
    for field in ("invited_at", "created_at", "sent_at"):
        value = row.get(field)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def mask(email):
    """Hide the local part of an email address. Pure. Non-emails pass through."""
    text = str(email or "").strip()
    if "@" not in text:
        return text or "unknown"
    local, _, domain = text.partition("@")
    if not local:
        return text
    return local[0] + "***@" + domain


def project_roles(invite):
    """[(project_id, role)] carried by one invite. Pure.

    These take effect on acceptance, so they are part of what the invite is
    offering and part of what deleting it withdraws.
    """
    out = []
    for entry in (invite or {}).get("projects") or []:
        row = entry or {}
        out.append((str(row.get("id") or "unknown"),
                    str(row.get("role") or "member").strip().lower()))
    return out


def owner_grant(invite):
    """Does this invite hand over owner anywhere? Pure.

    True for a top-level owner role and for a project entry at owner, because
    an org reader with project owner is still an unclaimed grant of control.
    """
    row = invite or {}
    if str(row.get("role") or "").strip().lower() == OWNER:
        return True
    return any(role == OWNER for _, role in project_roles(row))


def member_emails(users):
    """Lowercased email addresses on the current roster. Pure."""
    out = set()
    for user in users or []:
        email = str((user or {}).get("email") or "").strip().lower()
        if email:
            out.add(email)
    return out


def classify(invite, members, now, stale_days=14):
    """Classify one invite. Pure. Returns (state, detail).

    expires_at is tested against the clock BEFORE status is tested against a
    string. A record can sit at "pending" past its own expiry, and that is the
    row every status filter misses.
    """
    row = invite or {}
    status = str(row.get("status") or "").strip().lower()
    email = str(row.get("email") or "").strip().lower()
    sent = sent_at(row)
    age = (int(now) - sent) // DAY if sent else None

    if status == "accepted":
        return ("accepted", "accepted%s"
                % ("" if age is None else ", sent %d day(s) ago" % age))

    if email and email in (members or set()):
        return ("already-a-member",
                "this address is already on the roster%s"
                % ("" if age is None else ", invite sent %d day(s) ago" % age))

    if status == "expired":
        return ("expired-uncollected",
                "expired and never cleaned up%s"
                % ("" if age is None else ", sent %d day(s) ago" % age))

    if status != "pending":
        return ("unknown-status",
                "status %r is not one this audit recognises" % status)

    expires = row.get("expires_at")
    try:
        expires = int(expires) if expires else None
    except (TypeError, ValueError):
        expires = None
    if expires and expires < int(now):
        return ("expired-but-still-pending",
                "still reads pending%s, and expires_at passed %d day(s) ago. A "
                "filter on status alone never returns this row."
                % ("" if age is None else " %d day(s) after it was sent" % age,
                   (int(now) - expires) // DAY))

    if age is not None and age >= stale_days:
        return ("pending-stale",
                "pending for %d day(s) and not yet past its expires_at" % age)

    return ("pending", "sent recently and still live")


def repair_lines(state, invite):
    """The repair for one classified invite. Pure. Printed, never performed."""
    row = invite or {}
    invite_id = str(row.get("id") or "unknown")
    lines = []
    if state not in FINDINGS:
        return lines
    if owner_grant(row):
        lines.append("this invite still offers owner rights. Read it before you "
                     "re-send anything: an uncollected owner grant only needs "
                     "access to one mailbox.")
    if state == "already-a-member":
        lines.append("this person is already in the roster. Delete the record; "
                     "there is no onboarding problem here.")
    elif state == "expired-but-still-pending":
        lines.append("the record is dead and still listed as pending. Delete it, "
                     "then decide separately whether to re-send.")
    elif state == "pending-stale":
        lines.append("ask whether they ever received it. The API has no delivery "
                     "status and cannot tell a filtered message from an ignored "
                     "one.")
    else:
        lines.append("expired and never cleaned up. Delete unless this person is "
                     "still expected.")
    grants = project_roles(row)
    if grants and state != "already-a-member":
        lines.append("re-send with the same projects[] entries (%s) or the new "
                     "invite grants less than the first one did."
                     % ", ".join("%s=%s" % g for g in grants))
    lines.append("DELETE /v1/organization/invites/%s" % invite_id)
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
    ap.add_argument("--stale-days", type=int, default=14,
                    help="days a pending invite may sit before it is flagged")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key; a "
                  "project key cannot read /v1/organization/*")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})
    now = int(time.time())

    members = member_emails(paged(s, "/organization/users", limit=100))
    invites = list(paged(s, "/organization/invites", limit=100))

    graded = [(invite, classify(invite, members, now, args.stale_days))
              for invite in invites]
    bad = [(invite, state, detail) for invite, (state, detail) in graded
           if state in FINDINGS]
    accepted = sum(1 for _, (state, _) in graded if state == "accepted")

    log.info("%d invite(s), %d accepted, %d finding(s)",
             len(invites), accepted, len(bad))

    bad.sort(key=lambda row: (0 if owner_grant(row[0]) else 1,
                              SEVERITY.get(row[1], 9),
                              str(row[0].get("email") or "")))
    for invite, state, detail in bad:
        log.warning("%-26s %-22s role=%-7s %s", state,
                    mask(invite.get("email")),
                    str(invite.get("role") or "?"), detail)
        grants = project_roles(invite)
        if grants:
            log.warning("  grants: %s",
                        ", ".join("%s=%s" % g for g in grants))
        for line in repair_lines(state, invite):
            log.warning("  repair: %s", line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
