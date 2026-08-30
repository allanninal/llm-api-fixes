"""Report OpenAI API keys whose owner no longer has access to the project.

Read only. GET requests and nothing else. This one needs an ORGANIZATION ADMIN
key (sk-admin-...), because every /v1/organization/* endpoint rejects a project
key outright; an admin key provisioned read-only is enough and is what you
should give it. The repair is printed, never performed: a key on this list may
still be carrying production traffic, and revoking it before you know that is
how a cleanup becomes an outage.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_orphaned_key_audit")

API = "https://api.openai.com/v1"
DAY = 86400

# Worst first, so the report leads with the key that is still serving traffic
# rather than with the harmless one that has never been used.
SEVERITY = {"serving": 4, "orphaned": 3, "unknown": 2, "dormant": 1, "in-force": 0}


def owner_label(key):
    """Best identity available for whoever owns a key. Pure.

    owner.type is "user" or "service_account"; only the user branch carries an
    email, and a service account carries a name instead. Falling back to the
    type rather than to "?" keeps the row readable when neither is populated.
    """
    owner = key.get("owner") or {}
    user = owner.get("user") or {}
    account = owner.get("service_account") or {}
    return (user.get("email") or user.get("name") or account.get("name")
            or owner.get("type") or "unknown owner")


def verdict(key, now, hot_days=7):
    """Classify one organization.project.api_key object.

    Pure, so the rules can be read and tested without an admin credential and
    without a network. `now` is a unix timestamp, and so are `last_used_at` and
    `created_at` on this object; `last_used_at` is null on a key that has never
    authenticated a request.

    Returns (state, detail).
    """
    raw = key.get("owner_project_access")
    if raw is None:
        return ("unknown",
                "no owner_project_access on this object: ask for it explicitly "
                "with owner_project_access=any and re-read, rather than taking "
                "the absence for active")
    access = str(raw).strip().lower()
    if access == "active":
        return ("in-force", "owner still has access to this project")
    if access != "inactive":
        return ("unknown", "unrecognised owner_project_access %r" % (raw,))

    last = key.get("last_used_at")
    if last is None:
        return ("dormant",
                "owner has lost project access and this key has never "
                "authenticated a request. Nothing depends on it, so it is the "
                "safe one to revoke first.")
    age = (int(now) - int(last)) // DAY
    if age <= hot_days:
        return ("serving",
                "owner has lost project access and the key authenticated a "
                "request %d day(s) ago. Something in production is still "
                "holding it: re-issue before you revoke." % age)
    return ("orphaned",
            "owner has lost project access; last used %d day(s) ago" % age)


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=30)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI: /v1/organization/* needs an "
                         "organization admin key (sk-admin-...), not a project key")
    r.raise_for_status()
    return r.json()


def paged(session, path, **params):
    """Walk a cursor-paginated admin listing."""
    params.setdefault("limit", 100)
    while True:
        page = get(session, path, **params)
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params["after"] = page.get("last_id") or data[-1].get("id")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hot-days", type=int, default=7,
                    help="a key used inside this many days counts as live traffic")
    ap.add_argument("--all-keys", action="store_true",
                    help="read every key (owner_project_access=any), not only the "
                         "inactive-owner ones, for a full inventory")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key (sk-admin-...); "
                  "a project key cannot read /v1/organization/*")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})
    now = int(time.time())
    scope = "any" if args.all_keys else "inactive"

    rows = []
    projects = 0
    # include_archived=true, because an archived project still holds live keys
    # and is absent from the default listing.
    for project in paged(s, "/organization/projects", include_archived="true"):
        projects += 1
        path = "/organization/projects/%s/api_keys" % project["id"]
        for key in paged(s, path, owner_project_access=scope):
            state, detail = verdict(key, now, args.hot_days)
            rows.append((state, detail, project, key))

    rows.sort(key=lambda r: (-SEVERITY.get(r[0], 2), -(r[3].get("last_used_at") or 0)))

    bad = 0
    for state, detail, project, key in rows:
        line = "%-9s %s / %s  %s  %s" % (
            state, project.get("name") or project["id"], owner_label(key),
            key.get("redacted_value") or "?", detail)
        if state == "in-force":
            log.info(line)
            continue
        bad += 1
        log.warning(line)
        log.warning("  repair: mint a replacement under a service account, deploy "
                    "it, confirm last_used_at stops moving, then remove this one: "
                    "DELETE %s/organization/projects/%s/api_keys/%s",
                    API, project["id"], key.get("id"))

    log.info("%d key(s) read across %d project(s), %d whose owner no longer has "
             "project access", len(rows), projects, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
