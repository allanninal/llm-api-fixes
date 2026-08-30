"""Find production keys whose owner is a person rather than a service account.

Read only. Three GETs against the OpenAI Administration API with an admin key:
the project list, each project's keys and service accounts, and the cost report
grouped by api_key_id. Nothing is created, changed or removed, and no key value
is printed.

The finding is the ownership type. This is not a concentration check: the
verdict function is never given an organization total, so the share of the bill
a key holds cannot influence its grade. Two personal keys splitting production
spend evenly are two findings here.

Anthropic is not covered, and not because it was skipped. Its key object has no
owner-type distinction between a person's credential and a service one, and it
has no project service-account object to compare against. created_by records
who minted a key, which is a different question.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_user_owned_key_audit")

API = "https://api.openai.com/v1"

USER = "user"
SERVICE_ACCOUNT = "service_account"
UNKNOWN = "unknown"

IN_PRODUCTION = "personal-key-in-production"
IDLE = "personal-key-idle"
UNATTRIBUTABLE = "unattributable-owner"
FINE = "service-account-key"
FINDINGS = (IN_PRODUCTION, IDLE, UNATTRIBUTABLE)


def safe_hint(value):
    """Return a key hint that is safe to print. Pure.

    The API returns redacted_value already redacted. Anything that does not
    look redacted is withheld, because an audit script printing a live
    credential into a log is the one mistake here that cannot be undone.
    """
    text = str(value or "").strip()
    if not text:
        return "(no hint)"
    if ("..." not in text and "*" not in text) or len(text) > 40:
        return "(hint withheld)"
    return text


def owner_kind(key):
    """Is this key owned by a person or by a service account? Pure.

    An absent or unrecognised owner block becomes "unknown" and is never folded
    into either camp. A credential nobody can attribute is a finding in its own
    right, and quietly counting it as a service account would hide it.
    """
    owner = (key or {}).get("owner")
    if not isinstance(owner, dict):
        return UNKNOWN
    kind = str(owner.get("type") or "").strip().lower()
    return kind if kind in (USER, SERVICE_ACCOUNT) else UNKNOWN


def owner_label(key):
    """A printable identity for the key's owner. Pure. Never a key value."""
    owner = (key or {}).get("owner")
    if not isinstance(owner, dict):
        return "(no owner block)"
    if owner_kind(key) == USER:
        user = owner.get("user") if isinstance(owner.get("user"), dict) else {}
        return str(user.get("email") or user.get("name") or user.get("id")
                   or "(user, unnamed)")
    if owner_kind(key) == SERVICE_ACCOUNT:
        account = (owner.get("service_account")
                   if isinstance(owner.get("service_account"), dict) else {})
        return str(account.get("name") or account.get("id")
                   or "(service account, unnamed)")
    return "(owner type %r)" % str(owner.get("type"))


def fold_costs(pages):
    """Sum cost by api_key_id, keeping currency. Pure.

    Returns {api_key_id: {currency: amount}}. Currency is kept rather than
    dropped because an organization billed in more than one currency produces a
    meaningless number the moment the units are discarded.
    """
    out = {}
    for page in pages or []:
        for bucket in (page or {}).get("data") or []:
            for result in (bucket or {}).get("results") or []:
                key_id = (result or {}).get("api_key_id")
                amount = (result or {}).get("amount")
                if not key_id or not isinstance(amount, dict):
                    continue
                try:
                    value = float(amount.get("value") or 0)
                except (TypeError, ValueError):
                    continue
                currency = str(amount.get("currency") or "USD").upper()
                out.setdefault(str(key_id), {})
                out[str(key_id)][currency] = \
                    out[str(key_id)].get(currency, 0.0) + value
    return out


def spend_of(costs, key_id):
    """The largest single-currency amount recorded for one key. Pure.

    Used only to compare against a threshold. Taking the maximum rather than a
    sum keeps the comparison honest in a multi-currency organization without
    inventing an exchange rate.
    """
    by_currency = (costs or {}).get(str(key_id or ""), {})
    return max(by_currency.values()) if by_currency else 0.0


def spend_line(costs, key_id, days):
    """A printable spend summary for one key. Pure. Never adds currencies."""
    by_currency = (costs or {}).get(str(key_id or ""), {})
    if not by_currency:
        return "no cost rows in %d day(s)" % days
    parts = ["%.2f %s" % (value, currency)
             for currency, value in sorted(by_currency.items())]
    return "%s over %d day(s)" % (" + ".join(parts), days)


def verdict(key, key_spend, service_account_count, min_spend=1.0):
    """Classify one key by who owns it. Pure. Returns (state, detail).

    Deliberately not given an organization total. The share of the bill this
    key holds is not an input, cannot be an input, and is the subject of a
    different note; a personal key carrying three per cent of production spend
    grades exactly as a personal key carrying ninety-five.
    """
    kind = owner_kind(key)
    if kind == SERVICE_ACCOUNT:
        return (FINE, "owned by a service account")
    if kind == UNKNOWN:
        return (UNATTRIBUTABLE,
                "the owner block is missing or its type is unrecognised, so "
                "nobody can say whose lifecycle this credential is attached to")
    if float(key_spend or 0) >= float(min_spend):
        return (IN_PRODUCTION,
                "a person owns a credential carrying production spend%s"
                % (", in a project with no service accounts at all"
                   if not service_account_count else ""))
    return (IDLE,
            "owned by a person and carrying no measurable spend, so this is a "
            "revocation rather than a migration")


def project_note(project_name, user_owned_spending, service_account_count):
    """The project-level finding, printed once per project. Pure.

    A project with spending personal keys and an empty service-account roster
    has not made a mistake in one place. It has never had the mechanism, and
    the repair is different: create the first one rather than migrate to the
    existing ones.
    """
    if user_owned_spending and not service_account_count:
        return ("project %s: no service accounts at all, and %d user-owned "
                "key(s) are spending" % (project_name, user_owned_spending))
    return None


def migration_plan(project_id, key_id, key_name):
    """The ordered cutover, printed and never performed. Pure.

    Revocation is last because the service-account key value is returned once
    at creation and because removing the old key before traffic moves is an
    outage rather than a rotation.
    """
    return [
        "create a service account for the service: an admin POST to "
        "/v1/organization/projects/%s/service_accounts with a name that "
        "matches the deployable unit, not the person." % project_id,
        "mint its key under /v1/organization/projects/%s/service_accounts/"
        "{service_account_id}/api_keys. The value is returned exactly once, "
        "so capture it into the secret store in the same step." % project_id,
        "deploy the new value, then re-read the cost report grouped by "
        "api_key_id and confirm the spend has moved off %s (%s)."
        % (key_name, key_id),
        "only then revoke the old key with a DELETE on "
        "/v1/organization/projects/%s/api_keys/%s." % (project_id, key_id),
    ]


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an admin "
                         "key (sk-admin-), not a project key" % r.status_code)
    r.raise_for_status()
    return r.json()


def paged(session, path, params):
    """Walk an administration listing on has_more / last_id."""
    params = dict(params)
    while True:
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of cost to join by api_key_id (default 30)")
    ap.add_argument("--min-spend", type=float, default=1.0,
                    help="spend above which a personal key is production")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an admin key (sk-admin-) with read "
                  "scopes; a project key cannot read /v1/organization/*")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})

    start = int((dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=args.days)).timestamp())
    costs = fold_costs(paged(s, "/organization/costs",
                             {"start_time": start, "limit": min(args.days, 180),
                              "group_by": "api_key_id"}))

    projects = collect(s, "/organization/projects",
                       {"limit": 100, "include_archived": "true"})

    total_keys = 0
    user_owned = 0
    findings = 0
    empty_rosters = 0

    for project in projects:
        pid = project.get("id")
        if not pid:
            continue
        name = project.get("name") or pid
        keys = collect(s, "/organization/projects/%s/api_keys" % pid,
                       {"limit": 100, "owner_project_access": "any"})
        accounts = collect(s, "/organization/projects/%s/service_accounts" % pid,
                           {"limit": 100})
        total_keys += len(keys)

        graded = []
        for key in keys:
            key_spend = spend_of(costs, key.get("id"))
            state, detail = verdict(key, key_spend, len(accounts), args.min_spend)
            graded.append((key, state, detail, key_spend))
            if owner_kind(key) == USER:
                user_owned += 1

        spending = sum(1 for _, state, _, _ in graded if state == IN_PRODUCTION)
        note = project_note(name, spending, len(accounts))
        if note:
            empty_rosters += 1
            log.warning(note)

        for key, state, detail, _ in sorted(
                graded, key=lambda row: -spend_of(costs, row[0].get("id"))):
            if state not in FINDINGS:
                continue
            findings += 1
            log.warning("%-27s %-12s %-12s %s  %-24s %s", state, name,
                        key.get("name") or "(unnamed)",
                        safe_hint(key.get("redacted_value")), owner_label(key),
                        spend_line(costs, key.get("id"), args.days))
            log.warning("  detail: %s", detail)
            if state == IN_PRODUCTION:
                for step in migration_plan(pid, key.get("id"),
                                           key.get("name") or "(unnamed)"):
                    log.warning("  repair: %s", step)
            elif state == IDLE:
                log.warning("  repair: no traffic behind this one, so it is a "
                            "revocation rather than a migration.")

    log.info("%d project(s), %d key(s), %d owned by a user", len(projects),
             total_keys, user_owned)
    log.info("%d finding(s), %d project(s) with no service accounts",
             findings, empty_rosters)
    log.info("share of the bill is not part of any verdict above: that is a "
             "different note and a different repair")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
