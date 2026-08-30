"""Report whether anything would stop a runaway OpenAI bill.

Read only. GET requests and nothing else: OPENAI_ADMIN_KEY must be an
organization admin key (sk-admin-...) with read scopes, because every
/v1/organization endpoint rejects a project key. The repair is printed, never
performed, because a script should not be the thing that changes what an
organization is allowed to spend.
"""
import argparse
import calendar
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_spend_limit_audit")

API = "https://api.openai.com/v1"


def threshold_dollars(limit):
    """Read threshold_amount as dollars, or None when no limit is configured.

    The field is in CENTS. A limit typed as 500 meaning five hundred dollars is
    five dollars and takes production down inside the hour, so the conversion
    lives in one named place rather than inline at three call sites.
    """
    if not isinstance(limit, dict):
        return None
    obj = limit.get("spend_limit") if isinstance(limit.get("spend_limit"), dict) else limit
    raw = obj.get("threshold_amount")
    if raw is None or raw == "":
        return None
    try:
        return float(raw) / 100.0
    except (TypeError, ValueError):
        return None


def projected_month_end(spent, now):
    """Pro-rate month-to-date spend to a month-end figure. Pure, clock injected.

    Spend on the third of the month says almost nothing about the month. The
    fraction of the month elapsed is measured to the hour, so the first day
    does not divide by zero and does not produce an absurd projection either.
    """
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    elapsed_hours = (now.day - 1) * 24 + now.hour + now.minute / 60.0
    total_hours = days_in_month * 24.0
    fraction = max(elapsed_hours / total_hours, 1.0 / total_hours)
    return spent / fraction


def unknown_recipients(alerts, known_emails):
    """Alert recipients who are not members of the organization any more.

    An alert addressed to someone who left is not an alert. Returned sorted so
    the output is stable between runs.
    """
    known = {str(e).strip().lower() for e in known_emails}
    missing = set()
    for a in alerts:
        channel = a.get("notification_channel") or {}
        for r in channel.get("recipients") or []:
            if str(r).strip().lower() not in known:
                missing.add(str(r))
    return sorted(missing)


def verdict(limit, alerts, spent, now):
    """Classify one scope's protection against a runaway. Pure.

    Returns (state, detail). Ordered deliberately: an absent limit and an
    inactive one have the same effect on the bill and different repairs, and a
    ceiling that can never fire is a separate finding from one that already has.
    """
    projected = projected_month_end(spent, now)
    threshold = threshold_dollars(limit)
    money = "$%.2f month-to-date, projecting $%.2f" % (spent, projected)

    if threshold is None:
        return ("no-limit",
                "%s, and no spend limit is configured. Nothing in the platform "
                "will refuse a request no matter how much a runaway spends."
                % money)

    status = ""
    if isinstance(limit, dict):
        obj = limit.get("spend_limit") if isinstance(limit.get("spend_limit"), dict) else limit
        enforcement = obj.get("enforcement") or {}
        status = str(enforcement.get("status") or "")

    if status and status != "enforcing":
        return ("not-enforcing",
                "%s. A limit of $%.2f exists but enforcement.status is %r, so it "
                "displays and does not brake." % (money, threshold, status))

    if threshold * 100 <= projected:
        return ("cents-mistake",
                "%s, against a limit of $%.2f. threshold_amount is in cents: a "
                "value this far below the run rate is almost always a figure "
                "typed as dollars, which is 100x too low and will page you "
                "immediately." % (money, threshold))

    if threshold <= spent:
        return ("breached",
                "%s, against a limit of $%.2f. Requests are already being "
                "refused with 429 organization_spend_limit_exceeded."
                % (money, threshold))

    if threshold <= projected:
        return ("will-breach",
                "%s, against a limit of $%.2f. At this run rate the brake "
                "engages before the interval resets." % (money, threshold))

    if threshold >= projected * 5:
        return ("ceiling-too-high",
                "%s, against a limit of $%.2f. A ceiling more than five times "
                "the run rate cannot fire in time to be useful."
                % (money, threshold))

    if not alerts:
        return ("no-alerts",
                "%s, with a limit of $%.2f enforcing and no spend alerts. A "
                "brake with no warning light: the first signal is production "
                "returning 429." % (money, threshold))

    return ("guarded",
            "%s, limit $%.2f, %d alert(s)" % (money, threshold, len(alerts)))


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=30)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization endpoints need an "
                         "organization admin key, not a project key"
                         % r.status_code)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def month_to_date(session, now, project_id=None):
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    params = {"start_time": int(start.timestamp()), "limit": 31}
    if project_id:
        params["project_ids"] = project_id
    costs = get(session, "/organization/costs", **params)
    total = 0.0
    for b in costs.get("data", []):
        for r in b.get("results", []) or []:
            total += float((r.get("amount") or {}).get("value") or 0.0)
    return total


def report(scope, limit, alerts, spent, now):
    state, detail = verdict(limit, alerts, spent, now)
    line = "%-16s %-24s %s" % (state, scope, detail)
    if state == "guarded":
        log.info(line)
        return 0
    log.warning(line)
    projected = projected_month_end(spent, now)
    suggested = int(round(projected * 2)) * 100
    log.warning("  repair, to run yourself: POST %s/organization/spend_limit "
                "with a body of {\"threshold_amount\": %d, \"currency\": "
                "\"USD\", \"interval\": \"month\"} -- that is %d cents, "
                "which is $%.2f.", API, suggested, suggested, suggested / 100.0)
    log.warning("  then alerts at 50%%, 75%% and 90%% of it via "
                "%s/organization/spend_alerts, with a real recipients list.", API)
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--projects", action="store_true",
                    help="also read the per-project limit and alerts")
    ap.add_argument("--max-projects", type=int, default=25,
                    help="stop after this many projects")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key with read "
                  "scopes; project keys are rejected by /v1/organization/*)")
        return 2

    now = dt.datetime.now(dt.timezone.utc)
    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})

    limit = get(s, "/organization/spend_limit")
    alerts = get(s, "/organization/spend_alerts", limit=100).get("data", [])
    spent = month_to_date(s, now)

    scopes = 1
    bad = report("organization", limit, alerts, spent, now)

    users = get(s, "/organization/users", limit=100).get("data", [])
    stale = unknown_recipients(alerts, [u.get("email") for u in users])
    if stale:
        bad += 1
        log.warning("%-16s %-24s alert recipients not in the organization: %s",
                    "stale-recipient", "organization", ", ".join(stale))

    if args.projects:
        projects = get(s, "/organization/projects", limit=args.max_projects)
        for p in projects.get("data", [])[:args.max_projects]:
            pid = p.get("id")
            if not pid or str(p.get("status") or "active") != "active":
                continue
            scopes += 1
            plimit = get(s, "/organization/projects/%s/spend_limit" % pid)
            palerts = get(s, "/organization/projects/%s/spend_alerts" % pid,
                          limit=100).get("data", [])
            pspent = month_to_date(s, now, project_id=pid)
            bad += report(p.get("name") or pid, plimit, palerts, pspent, now)

    log.info("%d scope(s) checked, %d finding(s)", scopes, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
