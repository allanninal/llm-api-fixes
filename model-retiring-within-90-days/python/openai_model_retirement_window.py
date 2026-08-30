"""Turn OpenAI shutdown dates into a migration schedule, ordered by urgency.

Read only. GET requests and nothing else: the models list needs a project key
set to Read Only, and the optional traffic join needs an organization admin key
because usage belongs to the organization rather than to a project. The repair
is printed, never performed.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_model_retirement_window")

API = "https://api.openai.com/v1"

FLAGGED = ("urgent", "due", "expired", "unreadable-date")


def parse_day(value):
    """Read a shutdown_date into a date, or None when it cannot be read."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw.split("T")[0])
    except ValueError:
        return None


def traffic_note(requests_30d):
    """How the traffic column is described, including when there is none.

    None means the admin key was not supplied, which is different from zero and
    has to read differently, or an unmeasured id looks like an unused one.
    """
    if requests_30d is None:
        return ("traffic unknown: no admin key, so this is ordered by date "
                "alone")
    if requests_30d == 0:
        return ("no requests in the last 30 days, so this is probably a string "
                "in a config file or a monthly job rather than live traffic")
    return "%d request(s) in the last 30 days" % (requests_30d,)


def plan(model, today, window_days=90, urgent_within=30, requests_30d=None):
    """Classify one models-list entry into a place in the migration schedule.

    Pure, and both thresholds are arguments: the right window is however long a
    model change takes to evaluate and roll out where you work, and that is not
    a constant this script gets to choose. Returns (state, detail).
    """
    raw = model.get("shutdown_date")
    if raw is None or str(raw).strip() == "":
        return ("unscheduled",
                "no shutdown date published today. Re-read the field rather "
                "than trusting this answer for a quarter.")

    day = parse_day(raw)
    if day is None:
        return ("unreadable-date",
                "shutdown_date is %r, which this script will not guess at."
                % (raw,))

    days = (day - today).days
    note = traffic_note(requests_30d)
    if days < 0:
        return ("expired",
                "shut down %d day(s) ago on %s. This is past planning; calls "
                "naming it are already failing. %s"
                % (-days, day.isoformat(), note))
    if days <= urgent_within:
        return ("urgent",
                "%d day(s) left, shutting down %s. Under %d days is scheduling "
                "work now, not next cycle. %s"
                % (days, day.isoformat(), urgent_within, note))
    if days <= window_days:
        return ("due",
                "%d day(s) left, shutting down %s. Inside the %d day window. %s"
                % (days, day.isoformat(), window_days, note))
    return ("later",
            "%d day(s) left, shutting down %s. Outside the window; nothing to "
            "do yet. %s" % (days, day.isoformat(), note))


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI on %s: check the key, and that an "
                         "organization admin key is used for /organization/*"
                         % (r.status_code, path))
    r.raise_for_status()
    return r.json()


def usage_by_model(admin_key, days):
    """Sum num_model_requests per model over the window.

    Needs an organization admin key: the usage endpoints reject project keys
    outright. Returns {} when no key was given, which the caller reports as
    unknown rather than as zero.
    """
    if not admin_key:
        return {}
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + admin_key})
    start = int((dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=days)).timestamp())
    totals = {}
    params = {"start_time": start, "bucket_width": "1d",
              "group_by[]": "model", "limit": 31}
    while True:
        page = get(session, "/organization/usage/completions", **params)
        for bucket in page.get("data", []):
            for row in bucket.get("results", []):
                name = row.get("model")
                if name:
                    totals[name] = totals.get(name, 0) + int(
                        row.get("num_model_requests") or 0)
        if not page.get("has_more"):
            break
        params["page"] = page.get("next_page")
        if not params["page"]:
            break
    return totals


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", type=int, default=90,
                    help="days ahead to treat as inside the migration window")
    ap.add_argument("--urgent-within", type=int, default=30,
                    help="days ahead that count as urgent rather than due")
    ap.add_argument("--usage-days", type=int, default=30,
                    help="days of usage to sum when an admin key is available")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY (a project key set to Read Only)")
        return 2
    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.warning("OPENAI_ADMIN_KEY is not set: the report will be ordered by "
                    "date alone, with no idea which ids carry traffic")

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})
    models = get(session, "/models").get("data", [])
    dated = [m for m in models if str(m.get("shutdown_date") or "").strip()]

    totals = usage_by_model(admin, args.usage_days)

    rows = []
    today = dt.date.today()
    for model in dated:
        model_id = str(model.get("id") or "?")
        seen = totals.get(model_id) if admin else None
        if admin and seen is None:
            seen = 0
        state, detail = plan(model, today, args.window, args.urgent_within, seen)
        rows.append((parse_day(model.get("shutdown_date")) or dt.date.max,
                     -(seen or 0), state, model_id, detail))

    flagged = 0
    for _day, _neg, state, model_id, detail in sorted(rows):
        line = "%-14s %s  %s" % (state, model_id, detail)
        if state in FLAGGED:
            flagged += 1
            log.warning(line)
            log.warning("  repair: pin the successor from the deprecations page, "
                        "then re-run this against the new id so its own date is "
                        "on the calendar before it is a surprise")
        else:
            log.info(line)

    log.info("%d dated model(s), %d inside a %d day window",
             len(dated), flagged, args.window)
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
