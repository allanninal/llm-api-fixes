"""Report live API keys sitting inside archived OpenAI projects.

Read only. GET requests and nothing else, with an ORGANIZATION ADMIN key
(sk-admin-...) because /v1/organization/* rejects project keys; read-only admin
scopes are enough. The repair is printed, never performed.

Archiving a project hides it from the default listing without revoking anything
inside it, so the parameter below is the whole point of the script.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_archived_project_keys")

API = "https://api.openai.com/v1"
DAY = 86400

TRUTHY = ("true", "1", "yes", "on")


def covers_archived(params):
    """True when a projects listing will actually include archived projects.

    Pure. include_archived defaults to false, so a key audit that never passes
    it is auditing a subset of the organization and reporting a clean result
    over it. Accepts the bool and the query-string spelling, because the value
    reaches the API as a string either way.
    """
    value = params.get("include_archived")
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in TRUTHY


def verdict(project, keys, now):
    """Classify one project against the keys found inside it.

    Pure, so the comparison between a key's last_used_at and the project's
    archived_at is testable without an admin credential. All three timestamps
    are unix seconds; last_used_at is null on a key that has never been used.

    Returns (state, detail).
    """
    status = str(project.get("status") or "").strip().lower()
    archived_at = project.get("archived_at")
    if status != "archived" and archived_at is None:
        return ("active", "not archived; outside the scope of this check")

    keys = list(keys or [])
    if not keys:
        return ("clean", "archived, and holds no API keys")

    used_after = [k for k in keys
                  if k.get("last_used_at") and archived_at
                  and int(k["last_used_at"]) > int(archived_at)]
    if used_after:
        newest = max(int(k["last_used_at"]) for k in used_after)
        return ("still-serving",
                "%d of %d live key(s) authenticated a request after the project "
                "was archived, the most recent %d day(s) ago. This project is "
                "closed on paper and running in fact."
                % (len(used_after), len(keys), (int(now) - newest) // DAY))

    ever_used = [k for k in keys if k.get("last_used_at")]
    if ever_used:
        newest = max(int(k["last_used_at"]) for k in ever_used)
        return ("live-keys",
                "%d live key(s) inside an archived project, last used %d day(s) "
                "ago. Nothing has needed them since the archive."
                % (len(keys), (int(now) - newest) // DAY))
    return ("dormant-keys",
            "%d live key(s) inside an archived project, none of which has ever "
            "authenticated a request" % len(keys))


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=30)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI: /v1/organization/* needs an "
                         "organization admin key (sk-admin-...), not a project key")
    r.raise_for_status()
    return r.json()


def paged(session, path, **params):
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
    ap.add_argument("--show-active", action="store_true",
                    help="also print the projects that are not archived")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key (sk-admin-...); "
                  "a project key cannot read /v1/organization/*")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})
    now = int(time.time())

    listing = {"limit": 100, "include_archived": "true"}
    # Stated out loud, because the silent version of this mistake is what the
    # note is about: a sweep that omits the parameter reports a clean subset.
    log.info("listing covers archived projects: %s",
             "yes" if covers_archived(listing) else "NO, this audit is partial")

    projects = list(paged(s, "/organization/projects", **listing))
    archived = 0
    exposed = 0
    for project in projects:
        keys = []
        if str(project.get("status") or "").lower() == "archived" \
                or project.get("archived_at") is not None:
            archived += 1
            keys = list(paged(s, "/organization/projects/%s/api_keys" % project["id"],
                              owner_project_access="any"))
        state, detail = verdict(project, keys, now)
        line = "%-13s %s  %s" % (state, project.get("name") or project["id"], detail)
        if state in ("active", "clean"):
            if state == "clean" or args.show_active:
                log.info(line)
            continue
        exposed += len(keys)
        log.warning(line)
        for key in keys:
            log.warning("  repair: DELETE %s/organization/projects/%s/api_keys/%s  (%s)",
                        API, project["id"], key.get("id"),
                        key.get("redacted_value") or key.get("name") or "unnamed")
        log.warning("  and check the spend: GET %s/organization/costs"
                    "?start_time=<now-30d>&group_by=project_id", API)

    log.info("%d project(s), %d archived, %d live key(s) inside them",
             len(projects), archived, exposed)
    return 1 if exposed else 0


if __name__ == "__main__":
    sys.exit(main())
