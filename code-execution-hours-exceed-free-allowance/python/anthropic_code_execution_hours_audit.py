"""Report that Claude code execution has spent its free container hours.

Read only. GET requests and nothing else: ANTHROPIC_ADMIN_KEY must be an Admin
API key (sk-ant-admin...), which can be provisioned read-only. A workspace key
is rejected by every /v1/organizations/* path.

The finding has no threshold. Each organization gets 1,550 free container hours
a month and they are consumed before anything is billed, so a non-zero amount on
a code_execution cost row means the allowance is already gone. The messages
usage report does not carry this line under any grouping, which is why the check
lives on the cost report and reads usage only to prove the absence.

The repair is printed, never applied. Detaching a file from a request path is a
deploy, and changing a tool version changes what the model can do.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_code_execution_hours_audit")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# Free container hours per organization per month. Consumed before anything is
# charged, which is what makes any non-zero amount a finding on its own.
FREE_CONTAINER_HOURS = 1550

# Published price per container hour, and the per-execution minimum. Both are
# prices rather than fields, so they are constants you can override rather than
# something this script pretends to have read from the API.
HOURLY_RATE = 0.05
MINIMUM_MINUTES = 5

COST_TYPE = "code_execution"

FINDINGS = ("allowance-just-crossed", "allowance-spent", "allowance-dwarfed")


def amount(row):
    """Read a cost row's amount as a float. Pure.

    The cost report returns amount as a decimal STRING. Summing the raw values
    concatenates them in one language and throws in the other.
    """
    raw = (row or {}).get("amount")
    if raw is None or raw == "":
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def fold(cost_buckets):
    """Sum spend into {workspace_id: {cost_type: dollars}}. Pure.

    Every cost_type is kept, not just the one being looked for. A filter that
    discards what it does not recognise is how the next billable surface stays
    invisible for a quarter.
    """
    out = {}
    for bucket in cost_buckets or []:
        for result in bucket.get("results") or []:
            workspace = str(result.get("workspace_id") or "default workspace")
            kind = str(result.get("cost_type") or "unspecified")
            per_type = out.setdefault(workspace, {})
            per_type[kind] = per_type.get(kind, 0.0) + amount(result)
    return out


def code_execution_spend(folded, cost_type=COST_TYPE):
    """Dollars of code execution per workspace, zeros dropped. Pure."""
    return {workspace: types[cost_type]
            for workspace, types in (folded or {}).items()
            if types.get(cost_type, 0.0) > 0}


def billed_hours(dollars, rate=HOURLY_RATE):
    """Container hours behind a dollar amount. Pure.

    Rounded rather than left raw. Dollars are a decimal quantity and 0.05 has no
    exact binary representation, so 84.60 / 0.05 comes out at 1691.9999999999998
    and every later int() reports an hour that was never missing.
    """
    if rate <= 0:
        raise ValueError("rate must be positive")
    return round(max(0.0, float(dollars or 0.0)) / rate, 6)


def executions_ceiling(hours, minimum_minutes=MINIMUM_MINUTES):
    """The most executions that could account for these hours. Pure.

    A ceiling and never a count. Every execution bills at least the minimum, so
    twelve hours is one long job or 144 short ones and nothing above that. The
    API does not report an execution count, and inventing one would be worse
    than saying how far the number could go.
    """
    if minimum_minutes <= 0:
        raise ValueError("minimum_minutes must be positive")
    return int(max(0.0, float(hours or 0.0)) * 60.0 / minimum_minutes)


def usage_report_mentions_code_execution(pages):
    """Does the messages usage report carry this line anywhere? Pure.

    The answer today is no, under any grouping, in any field. The check is here
    so the script can state that as an observation rather than an assumption,
    and so it starts reporting the field on the day one appears.
    """
    for page in pages or []:
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                for name in result.keys():
                    if "code_execution" in str(name).lower():
                        return True
    return False


def verdict(dollars, free_hours=FREE_CONTAINER_HOURS, rate=HOURLY_RATE,
            marginal=5.0):
    """Classify one workspace's code execution spend. Pure. Returns (state, detail).

    There is no threshold to tune. The platform consumed the free allowance
    before it wrote the row, so zero means "inside the allowance" and anything
    else means "past it". The marginal band exists only to keep the language
    proportionate, not to suppress a finding.
    """
    spend = float(dollars or 0.0)
    if spend <= 0:
        return ("within-allowance",
                "no code_execution rows, so the free %d container hour(s) cover "
                "this workspace, or the tool is bundled free with a current web "
                "search or web fetch version" % free_hours)

    hours = billed_hours(spend, rate)
    shape = ("$%.2f billed, which is %d container hour(s) on top of the free %d"
             % (spend, int(hours), free_hours))

    if spend < marginal:
        return ("allowance-just-crossed",
                "%s. The allowance is gone; the overage is still small enough "
                "to fix before it is not." % shape)
    if hours > free_hours:
        return ("allowance-dwarfed",
                "%s. Billed hours now exceed the whole free allowance, so the "
                "free tier has stopped being a meaningful part of this bill."
                % shape)
    return ("allowance-spent",
            "%s. Container time is being charged on every execution from here "
            "to the end of the month." % shape)


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params):
    """Walk the paginated usage or cost report."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        yield page
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def month_start():
    """First of the current month, midnight UTC.

    The allowance resets monthly, so a rolling window straddles two of them and
    the arithmetic stops meaning anything.
    """
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0,
                       microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def rolling_start(days):
    """Midnight UTC, days ago, for a deliberate rolling read."""
    now = dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=0,
                    help="read a rolling window instead of the calendar month")
    ap.add_argument("--rate", type=float, default=HOURLY_RATE,
                    help="dollars per container hour (default 0.05)")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    start = rolling_start(args.days) if args.days else month_start()
    if args.days:
        log.warning("reading a rolling %d day window: the free allowance resets "
                    "monthly, so this may span two of them", args.days)

    cost_buckets = []
    for page in pages(s, "/organizations/cost_report",
                      {"starting_at": start, "limit": 31,
                       "group_by[]": ["description", "workspace_id"]}):
        cost_buckets.extend(page.get("data") or [])

    folded = fold(cost_buckets)
    spend = code_execution_spend(folded)

    bad = 0
    for workspace in sorted(folded, key=lambda w: -folded[w].get(COST_TYPE, 0.0)):
        state, detail = verdict(spend.get(workspace, 0.0), rate=args.rate)
        line = "%-24s %-16s %s" % (state, workspace, detail)
        if state not in FINDINGS:
            log.info(line)
            continue
        bad += 1
        log.warning(line)
        hours = billed_hours(spend[workspace], args.rate)
        log.warning("  at the %d minute minimum that is at most %d execution(s)",
                    MINIMUM_MINUTES, executions_ceiling(hours))
        log.warning("  repair: find the routes attaching files to requests that "
                    "never call the tool. Attached files are preloaded onto a "
                    "container and bill time whether the tool runs or not.")
        log.warning("  repair: bundling code execution with web_search_20260209 "
                    "or web_fetch_20260209 or later removes the charge entirely")

    seen = sorted({kind for types in folded.values() for kind in types})
    log.info("cost_type values in this window: %s", ", ".join(seen) or "none")

    usage = [next(iter(pages(s, "/organizations/usage_report/messages",
                             {"starting_at": start, "bucket_width": "1d",
                              "limit": 1})), {})]
    if usage_report_mentions_code_execution(usage):
        log.warning("the messages usage report now carries a code execution "
                    "field: read it, this script predates it")
    else:
        log.info("note: the messages usage report carries no code execution "
                 "field at all, which is why this check reads the cost report")

    log.info("%d workspace(s) with cost, %d finding(s)", len(folded), bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
