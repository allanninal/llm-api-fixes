"""Find API keys that no request has ever used.

Read only. Every request is a GET against the OpenAI Administration API or the
Anthropic Admin API. Nothing is created, changed or removed, and no key value
is printed: the providers return a redacted hint and that hint is all that
reaches the output.

The two providers answer the same question with different evidence, and the
difference is the point of the script. OpenAI carries last_used_at on the key
object, so "never used" is a field you read. Anthropic has no such field, so
"unused" has to be computed as a set difference between the active key list and
the api_key_id values appearing in the usage report, which reaches back only as
far as the report does. On Anthropic this script reports "unused in the last N
days" and will not say "never".
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("llm_idle_key_audit")

OPENAI = "https://api.openai.com/v1"
ANTHROPIC = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

NEVER = "never-used"
DORMANT = "dormant"
UNUSED_IN_WINDOW = "unused-in-window"
IN_USE = "in-use"
SEEN = "seen-in-window"
TOO_NEW = "too-new"
UNREADABLE = "unreadable-dates"
NOT_ACTIVE = "not-active"

FINDINGS = (NEVER, DORMANT, UNUSED_IN_WINDOW)

# Sort weight for the revocation queue. Lower goes first, and the order is by
# how safe the row is to delete rather than by how old it is: a key nothing has
# ever used cannot break anything when it is revoked, and a key that ran a job
# last spring can.
SAFETY = {NEVER: 0, UNUSED_IN_WINDOW: 1, DORMANT: 2}


def safe_hint(value):
    """Return a key hint that is safe to print. Pure.

    Both providers hand back a redacted form: OpenAI's redacted_value and
    Anthropic's partial_key_hint. This passes those through and refuses
    anything else, because the one unrecoverable mistake an audit script can
    make is to print a live credential into a log that then gets shipped
    somewhere. A hint with no ellipsis or star in it is not a hint.
    """
    text = str(value or "").strip()
    if not text:
        return "(no hint)"
    if "..." not in text and "*" not in text:
        return "(hint withheld)"
    if len(text) > 40:
        return "(hint withheld)"
    return text


def age_days(stamp, now):
    """Whole days between a timestamp and now. Pure. None when unreadable.

    Accepts a unix integer (OpenAI) or an RFC 3339 string (Anthropic). A reader
    that handles only one of the two treats every key from the other provider
    as ageless, which reads as "too new to judge" and quietly drops it from the
    report.
    """
    if stamp is None or stamp == "":
        return None
    when = None
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        when = dt.datetime.fromtimestamp(float(stamp), dt.timezone.utc)
    else:
        text = str(stamp).strip()
        if text.isdigit():
            when = dt.datetime.fromtimestamp(float(text), dt.timezone.utc)
        else:
            try:
                when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
    return int((now - when).total_seconds() // 86400)


def openai_verdict(key, now, never_after=30, dormant_after=90):
    """Classify one OpenAI key off last_used_at. Pure. Returns (state, detail).

    last_used_at is null on a key that has never authenticated anything, and a
    unix timestamp otherwise. Zero is treated as absent: it is not a plausible
    last-use time and reading it as one would date the key to 1970 and file it
    under dormant instead of never used.
    """
    row = key or {}
    created = age_days(row.get("created_at"), now)
    last = row.get("last_used_at")
    if last in (None, "", 0):
        if created is None:
            return (UNREADABLE,
                    "never used, and created_at cannot be read, so no age can "
                    "be given for it")
        if created < never_after:
            return (TOO_NEW,
                    "never used, but only %d day(s) old" % created)
        return (NEVER,
                "never used in the %d day(s) since it was created" % created)
    idle = age_days(last, now)
    if idle is None:
        return (UNREADABLE, "last_used_at is present but cannot be read")
    if idle >= dormant_after:
        return (DORMANT, "last used %d day(s) ago" % idle)
    return (IN_USE, "last used %d day(s) ago" % idle)


def anthropic_verdict(key, seen_ids, window_days, now, never_after=30):
    """Classify one Anthropic key off usage-report membership. Pure.

    There is no last_used_at on the Anthropic key object, so the strongest
    available claim is bounded by the usage report's window. The detail string
    says so on every row, because "unused in 30 days" and "never used" are
    different facts and only one of them is in evidence here.
    """
    row = key or {}
    status = str(row.get("status") or "active").strip().lower()
    if status != "active":
        return (NOT_ACTIVE, "status is %s, so it cannot authenticate" % status)
    created = age_days(row.get("created_at"), now)
    if created is not None and created < never_after:
        return (TOO_NEW, "only %d day(s) old" % created)
    if str(row.get("id") or "") in (seen_ids or set()):
        return (SEEN, "carried traffic inside the last %d day(s)" % window_days)
    return (UNUSED_IN_WINDOW,
            "no traffic in the last %d day(s). The Anthropic key object has no "
            "last_used_at field, so this is unused within the retrievable "
            "window and not a claim that it was never used." % window_days)


def audit_gaps(project_params, key_params):
    """Warn about a sweep that will silently under-report. Pure.

    Both parameters default to the narrower answer, and neither omission
    produces an error or a visibly short list. You get a clean report over a
    partial universe, which is the most convincing kind of wrong answer, so the
    check is an assertion in the code rather than a sentence in a comment.
    """
    gaps = []
    if str((project_params or {}).get("include_archived", "")).lower() != "true":
        gaps.append("include_archived is not true: archived projects are "
                    "omitted from the project listing, and every key inside "
                    "them with it")
    if str((key_params or {}).get("owner_project_access", "")) != "any":
        gaps.append("owner_project_access is not 'any': the key listing "
                    "applies membership visibility rules and can hide enabled "
                    "keys from this audit")
    return gaps


def seen_key_ids(pages):
    """Every non-null api_key_id in an Anthropic usage report. Pure."""
    out = set()
    for page in pages or []:
        for bucket in (page or {}).get("data") or []:
            for result in (bucket or {}).get("results") or []:
                key_id = (result or {}).get("api_key_id")
                if key_id:
                    out.add(str(key_id))
    return out


def revocation_order(rows):
    """Order findings by how safe each is to revoke. Pure.

    Never-used first: nothing has authenticated with it, so revocation cannot
    break traffic. Then the window-bounded Anthropic rows, then dormant keys
    longest-idle first, because those are the ones where something was built
    and somebody has to be asked before anything is deleted.
    """
    findings = [r for r in (rows or []) if (r or {}).get("state") in SAFETY]
    return sorted(findings,
                  key=lambda r: (SAFETY[r["state"]], -int(r.get("idle") or 0),
                                 str(r.get("name") or "")))


def repair_lines(state, row):
    """The repair for one classified key. Pure. Printed, never performed."""
    data = row or {}
    if state == NEVER:
        return [
            "nothing has ever authenticated with this key, so revoking it "
            "cannot break traffic. These are the safest credentials in the "
            "organization to remove.",
            "revoke with a DELETE on /v1/organization/projects/%s/api_keys/%s "
            "once somebody confirms what it was minted for."
            % (data.get("container") or "{project_id}", data.get("id") or "{key_id}"),
        ]
    if state == DORMANT:
        return [
            "something was built on this key and has since stopped calling. "
            "Ask what it was before revoking: annual jobs and disaster-recovery "
            "paths look exactly like this.",
            "if it is genuinely dead, revoke it and confirm last_used_at stops "
            "advancing rather than assuming it will.",
        ]
    if state == UNUSED_IN_WINDOW:
        return [
            "this is unused within the report window, not proven unused. "
            "Widen the window as far as the report allows before concluding "
            "anything, then archive the key rather than deleting it.",
            "the Anthropic key object carries an optional expires_at. Set one "
            "on the replacement so the next idle key expires itself.",
        ]
    return []


def get(session, url, params, who):
    r = session.get(url, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from %s: this endpoint needs an administration "
                         "key, not a project or workspace key"
                         % (r.status_code, who))
    r.raise_for_status()
    return r.json()


def openai_paged(session, path, params):
    """Walk an OpenAI administration listing on has_more / last_id."""
    params = dict(params)
    while True:
        page = get(session, OPENAI + path, params, "OpenAI")
        yield page
        if not page.get("has_more") or not page.get("last_id"):
            return
        params["after"] = page["last_id"]


def anthropic_paged(session, path, params):
    """Walk an Anthropic Admin listing on has_more / last_id."""
    params = dict(params)
    while True:
        page = get(session, ANTHROPIC + path, params, "Anthropic")
        yield page
        if not page.get("has_more") or not page.get("last_id"):
            return
        params["after_id"] = page["last_id"]


def anthropic_report(session, path, params):
    """Walk the Anthropic usage report on has_more / next_page."""
    params = dict(params)
    while True:
        page = get(session, ANTHROPIC + path, params, "Anthropic")
        yield page
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def window_start(days, now):
    """Floor to midnight UTC: starting_at must sit on a bucket boundary."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sweep_openai(session, now, args):
    """Read every project, every project key and every admin key."""
    project_params = {"limit": 100, "include_archived": "true"}
    key_params = {"limit": 100, "owner_project_access": "any"}
    for gap in audit_gaps(project_params, key_params):
        log.warning("audit gap: %s", gap)

    projects = []
    for page in openai_paged(session, "/organization/projects", project_params):
        projects.extend(page.get("data") or [])
    archived = sum(1 for p in projects if p.get("status") == "archived")

    rows = []
    for project in projects:
        pid = project.get("id")
        if not pid:
            continue
        for page in openai_paged(session,
                                 "/organization/projects/%s/api_keys" % pid,
                                 key_params):
            for key in page.get("data") or []:
                state, detail = openai_verdict(key, now, args.never_after,
                                               args.dormant_after)
                rows.append({"provider": "openai", "state": state,
                             "detail": detail, "id": key.get("id"),
                             "name": key.get("name") or "(unnamed)",
                             "hint": safe_hint(key.get("redacted_value")),
                             "container": pid,
                             "label": project.get("name") or pid,
                             "idle": age_days(key.get("last_used_at"), now)
                                     or age_days(key.get("created_at"), now) or 0})

    admin_keys = []
    for page in openai_paged(session, "/organization/admin_api_keys", {"limit": 100}):
        admin_keys.extend(page.get("data") or [])
    for key in admin_keys:
        state, detail = openai_verdict(key, now, args.never_after,
                                       args.dormant_after)
        rows.append({"provider": "openai", "state": state, "detail": detail,
                     "id": key.get("id"), "name": key.get("name") or "(unnamed)",
                     "hint": safe_hint(key.get("redacted_value")),
                     "container": "organization", "label": "admin key",
                     "idle": age_days(key.get("last_used_at"), now)
                             or age_days(key.get("created_at"), now) or 0})

    log.info("openai: %d project(s) read, %d archived, %d key(s) including %d "
             "admin key(s)", len(projects), archived, len(rows), len(admin_keys))
    return rows


def sweep_anthropic(session, now, args):
    """Read the active key roster, then the usage report it must be joined to."""
    keys = []
    for page in anthropic_paged(session, "/organizations/api_keys",
                                {"status": "active", "limit": 1000}):
        keys.extend(page.get("data") or [])

    seen = seen_key_ids(anthropic_report(
        session, "/organizations/usage_report/messages",
        {"starting_at": window_start(args.days, now), "bucket_width": "1d",
         "limit": min(args.days + 1, 31), "group_by[]": ["api_key_id"]}))

    rows = []
    for key in keys:
        state, detail = anthropic_verdict(key, seen, args.days, now,
                                          args.never_after)
        rows.append({"provider": "anthropic", "state": state, "detail": detail,
                     "id": key.get("id"), "name": key.get("name") or "(unnamed)",
                     "hint": safe_hint(key.get("partial_key_hint")),
                     "container": key.get("id"), "label": "anthropic",
                     "idle": age_days(key.get("created_at"), now) or 0})

    log.info("anthropic: %d active key(s), %d seen in the usage report over "
             "%d day(s)", len(keys), len(seen), args.days)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--never-after", type=int, default=30,
                    help="days a never-used key must exist before it is a finding")
    ap.add_argument("--dormant-after", type=int, default=90,
                    help="days since last use that counts as dormant")
    ap.add_argument("--days", type=int, default=30,
                    help="Anthropic usage-report window, which bounds that half")
    args = ap.parse_args()

    openai_key = os.environ.get("OPENAI_ADMIN_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not openai_key and not anthropic_key:
        log.error("set OPENAI_ADMIN_KEY (sk-admin-, read scopes) or "
                  "ANTHROPIC_ADMIN_KEY (sk-ant-admin), or both; a project or "
                  "workspace key cannot read the administration endpoints")
        return 2

    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    if openai_key:
        s = requests.Session()
        s.headers.update({"Authorization": "Bearer " + openai_key})
        rows.extend(sweep_openai(s, now, args))
    if anthropic_key:
        s = requests.Session()
        s.headers.update({"x-api-key": anthropic_key,
                          "anthropic-version": ANTHROPIC_VERSION})
        rows.extend(sweep_anthropic(s, now, args))

    queue = revocation_order(rows)
    for row in queue:
        log.warning("%-16s %-14s %-18s %s  %s", row["state"], row["label"],
                    row["name"], row["hint"], row["detail"])
        for repair in repair_lines(row["state"], row):
            log.warning("  repair: %s", repair)

    log.info("%d key(s) read, %d finding(s)", len(rows), len(queue))
    log.info("no key value appears above: both providers return a redacted "
             "hint and the hint is all this script will print")
    return 1 if queue else 0


if __name__ == "__main__":
    sys.exit(main())
