"""Report whether OpenAI usage can be attributed to your customers at all.

Read only. GET requests and nothing else: OPENAI_ADMIN_KEY must be an
organization admin key (sk-admin-...) with read scopes.

The finding here is not a number, it is a fact about the reporting dimensions.
user_id on the Usage API is the org member or service account that owns the
calling API key. It is never an end-user identifier you supplied, and the
request-level `user` field does not reach this endpoint at all. So the repair is
architectural, it is forward-only, and it is printed rather than performed:
minting credentials for your tenants is not an audit's job.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_tenant_attribution_audit")

API = "https://api.openai.com/v1"

# The complete list of dimensions the platform can attribute along. Not a
# starting point: there is no fourth one, and that is the note.
DIMENSIONS = ("user_id", "api_key_id", "project_id")

FINDINGS = ("single-key", "keys-below-tenants")


def fold(pages):
    """Sum usage into the three dimensions the platform actually holds. Pure.

    Returns {"users": {id: requests}, "keys": {...}, "projects": {...},
    "requests": total}. Buckets with a null grouping value are counted into the
    total but not into a dimension, because a null there means "not attributed"
    and inventing a bucket for it would flatter the result.
    """
    out = {"users": {}, "keys": {}, "projects": {}, "requests": 0}
    for page in pages:
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                try:
                    n = int(result.get("num_model_requests") or 0)
                except (TypeError, ValueError):
                    n = 0
                out["requests"] += n
                for field, key in (("user_id", "users"), ("api_key_id", "keys"),
                                   ("project_id", "projects")):
                    value = result.get(field)
                    if value:
                        name = str(value)
                        out[key][name] = out[key].get(name, 0) + n
    return out


def classify(user_id, directory):
    """What kind of principal is this user_id? Pure.

    "service-account", "member", or "unresolved". The first two are the same
    finding wearing different clothes: both are your own principals, neither is
    a customer. The third is a different problem entirely.
    """
    entry = directory.get(str(user_id))
    if entry is None:
        return "unresolved"
    if entry.get("service_account"):
        return "service-account"
    return "member"


def unresolved(folded, directory):
    """user_ids generating usage that the org directory does not know. Pure.

    Sorted, so two runs print the same order. Usually empty; when it is not,
    something is calling the API as a principal nobody can name, which wants
    answering before the attribution question does.
    """
    return sorted(u for u in folded.get("users", {})
                  if classify(u, directory) == "unresolved")


def verdict(folded, directory, tenant_count=None):
    """Can this organization's usage be sliced per customer? Pure.

    Returns (state, detail). tenant_count comes from your database because the
    API has no concept of a tenant; without it the script can still report the
    cardinality and the fact that every principal is one of your own, which is
    most of the answer.
    """
    keys = folded.get("keys") or {}
    users = folded.get("users") or {}
    total = folded.get("requests") or 0

    if total <= 0 and not keys:
        return ("no-usage",
                "no completions usage in the window, so there is nothing to "
                "attribute yet")

    kinds = sorted({classify(u, directory) for u in users})
    principal_note = ("%d user_id value(s), all of them org members or service "
                      "accounts rather than customers" % len(users))
    if "unresolved" in kinds:
        principal_note = ("%d user_id value(s), of which some resolve to nobody "
                          "in the org directory" % len(users))

    if len(keys) == 1:
        return ("single-key",
                "1 api_key_id covers every request in the window. There is one "
                "bucket, so per-customer cost has no place to come from. %s."
                % principal_note)

    if tenant_count is None:
        return ("unknown-tenant-count",
                "%d distinct api_key_id value(s) and %d project(s). %s. Pass "
                "the tenant count to judge whether that is enough buckets."
                % (len(keys), len(folded.get("projects") or {}), principal_note))

    if len(keys) < tenant_count:
        return ("keys-below-tenants",
                "%d distinct api_key_id value(s) against %d tenant(s). Cost per "
                "customer is unrecoverable by construction: the finest slice the "
                "platform can offer is one key, and there are fewer keys than "
                "customers. %s." % (len(keys), tenant_count, principal_note))

    return ("segmented",
            "%d distinct api_key_id value(s) for %d tenant(s), so the platform "
            "can slice finely enough. Confirm your key-to-tenant map is current."
            % (len(keys), tenant_count))


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI: OPENAI_ADMIN_KEY must be an "
                         "organization admin key, not a project key")
    if r.status_code == 403:
        raise SystemExit("403 from OpenAI: the key is not authorised for "
                         "/v1/organization")
    r.raise_for_status()
    return r.json()


def usage_pages(session, start_time, days, max_pages=20):
    params = {"start_time": start_time, "bucket_width": "1d", "limit": days,
              "group_by": list(DIMENSIONS)}
    for _ in range(max_pages):
        page = get(session, "/organization/usage/completions", params)
        yield page
        cursor = page.get("next_page")
        if not cursor:
            return
        params = dict(params, page=cursor)


def org_directory(session, max_pages=20):
    """The org's own principals, keyed by id, from GET /v1/organization/users."""
    out = {}
    params = {"limit": 100}
    for _ in range(max_pages):
        page = get(session, "/organization/users", params)
        data = page.get("data") or []
        for user in data:
            out[str(user.get("id"))] = {
                "name": user.get("name") or user.get("email") or "?",
                "service_account": bool(user.get("is_service_account")),
            }
        if not page.get("has_more") or not data:
            break
        params = {"limit": 100, "after": data[-1].get("id")}
    return out


def spend_by_key(session, start_time):
    out = {}
    page = get(session, "/organization/costs",
               {"start_time": start_time, "limit": 30, "group_by": "api_key_id"})
    for bucket in page.get("data") or []:
        for result in bucket.get("results") or []:
            key_id = str(result.get("api_key_id") or "unattributed")
            amount = (result.get("amount") or {}).get("value") or 0
            try:
                out[key_id] = out.get(key_id, 0.0) + float(amount)
            except (TypeError, ValueError):
                pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenants", type=int, default=None,
                    help="how many customers you serve; comes from your database")
    ap.add_argument("--days", type=int, default=7,
                    help="days of usage to fold (default 7)")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_ADMIN_KEY")
    if not key:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key with read scopes)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    now = dt.datetime.now(dt.timezone.utc)
    start = int((now - dt.timedelta(days=args.days)).timestamp())

    folded = fold(usage_pages(session, start, args.days))
    directory = org_directory(session)
    state, detail = verdict(folded, directory, args.tenants)

    log.info("%-20s %s", state, detail)

    for user_id in sorted(folded["users"], key=lambda u: -folded["users"][u]):
        kind = classify(user_id, directory)
        name = directory.get(user_id, {}).get("name", "not in the directory")
        log.info("  principal %-30s %-16s %s", user_id, kind, name)

    orphans = unresolved(folded, directory)
    if orphans:
        log.warning("  %d user_id value(s) resolve to nobody in the org "
                    "directory: %s", len(orphans), ", ".join(orphans))
        log.warning("  repair: find what is calling as these principals before "
                    "you touch the attribution question")

    if state in FINDINGS:
        spend = spend_by_key(session, int((now - dt.timedelta(days=30)).timestamp()))
        for key_id, amount in sorted(spend.items(), key=lambda kv: -kv[1])[:10]:
            log.warning("  30d spend  %-30s $%.2f", key_id, amount)
        log.warning("  repair: the Usage API cannot segment by end user. Mint "
                    "one key, or one project, per tenant or tenant tier via "
                    "/v1/organization/projects/{id}/service_accounts/{id}/api_keys "
                    "and attribute with group_by=api_key_id.")
        log.warning("  repair: this is forward-only and cannot backfill. Until "
                    "then, record each response's usage block against your own "
                    "tenant id and reconcile it against /v1/organization/costs.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
