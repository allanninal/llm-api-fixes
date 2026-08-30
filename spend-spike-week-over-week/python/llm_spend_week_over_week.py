"""Report a change in organization spend and say what shape the change is.

Read only. One paginated GET against whichever provider you point it at, and
nothing else. Both cost reports need an organization admin key: OpenAI's
rejects project keys, Anthropic's rejects workspace keys. Read-only admin keys
work and are what this should hold.

The repair is a spend limit and an alert, printed as an exact call for you to
run. This script never sets one: a script holding an admin key that can also
change your billing configuration is a worse tool than one that cannot.
"""
import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("llm_spend_week_over_week")

OPENAI_API = "https://api.openai.com/v1"
ANTHROPIC_API = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

EPOCH = date(1970, 1, 1)

FINDINGS = ("spike", "step", "ramp", "drop", "new-spend")


def _day_number(text):
    """An ISO day string to a day count since the epoch, or None."""
    try:
        return (date.fromisoformat(str(text)[:10]) - EPOCH).days
    except (TypeError, ValueError):
        return None


def _day_iso(number):
    return (EPOCH + timedelta(days=int(number))).isoformat()


def parse_cents(text):
    """Anthropic's decimal string of cents to integer millicents. Pure.

    Returns None on anything unparseable, which the caller skips rather than
    reading as zero. Integer millicents rather than a float because summing 56
    buckets of float cents is how a total ends up a cent adrift and an afternoon
    gets spent working out where.
    """
    raw = str(text if text is not None else "").strip()
    if not raw:
        return None
    negative = raw.startswith("-")
    if raw[:1] in ("+", "-"):
        raw = raw[1:]
    whole, _, frac = raw.partition(".")
    whole = whole or "0"
    frac = (frac + "000")[:3]
    if not whole.isdigit() or not frac.isdigit():
        return None
    value = int(whole) * 1000 + int(frac)
    return -value if negative else value


def daily_from_openai(buckets):
    """Fold GET /v1/organization/costs into {day: dollars}. Pure.

    amount.value is a float in dollars and start_time is a Unix timestamp, so
    the day key is the UTC date the bucket opened on.
    """
    days = {}
    for bucket in buckets or []:
        try:
            opened = int(bucket.get("start_time"))
        except (TypeError, ValueError):
            continue
        key = datetime.fromtimestamp(opened, timezone.utc).date().isoformat()
        for result in bucket.get("results") or []:
            try:
                value = float((result.get("amount") or {}).get("value") or 0.0)
            except (TypeError, ValueError):
                continue
            days[key] = round(days.get(key, 0.0) + value, 6)
    return days


def daily_from_anthropic(buckets):
    """Fold GET /v1/organizations/cost_report into {day: dollars}. Pure.

    amount is a decimal string in cents, so it is parsed as an exact number of
    millicents and only converted to dollars once, at the end.
    """
    days = {}
    for bucket in buckets or []:
        key = str(bucket.get("starting_at") or "")[:10]
        if _day_number(key) is None:
            continue
        for result in bucket.get("results") or []:
            millicents = parse_cents(result.get("amount"))
            if millicents is None:
                continue
            days[key] = days.get(key, 0) + millicents
    return {day: round(total / 100000.0, 4) for day, total in days.items()}


def weeks(daily, today, count=8):
    """Fold {day: dollars} into whole weeks, newest first. Pure.

    Returns [(first_day, last_day, dollars), ...]. Today is excluded, always:
    the current day's bucket is partial, and a comparison that includes it
    reports a fall in spend every time it runs before lunch. The anchor is the
    most recent complete day that carries data rather than yesterday, because
    both cost reports lag by a day or two and an empty tail would otherwise
    drag every week boundary with it.
    """
    end = _day_number(today)
    if end is None:
        return []
    totals = {}
    for key, value in (daily or {}).items():
        number = _day_number(key)
        if number is None or number >= end:
            continue
        try:
            totals[number] = totals.get(number, 0.0) + float(value or 0.0)
        except (TypeError, ValueError):
            continue
    if not totals:
        return []

    first = min(totals)
    stop = min(end, max(totals) + 1)
    out = []
    while len(out) < int(count):
        start = stop - 7
        if start < first:
            break
        total = sum(totals.get(day, 0.0) for day in range(start, stop))
        out.append((_day_iso(start), _day_iso(stop - 1), round(total, 2)))
        stop = start
    return out


def classify(totals, threshold=0.40, min_weeks=3):
    """Classify a list of weekly totals, newest first. Pure. (state, detail).

    Three ways for spend to be higher than it was, and they want three
    different people: a spike is one week and a job that ran once, a step is a
    new level that something shipped into, a ramp is growth that no
    week-over-week ratio will ever catch because it is already in the baseline.
    """
    series = []
    for value in totals or []:
        try:
            series.append(float(value))
        except (TypeError, ValueError):
            return ("unreadable", "a weekly total that is not a number")

    if len(series) < int(min_weeks):
        return ("too-short",
                "%d whole week(s) of history, which is not enough to call "
                "anything a change" % len(series))

    latest, prior = series[0], series[1:]
    baseline = sum(prior) / len(prior)
    if baseline <= 0:
        if latest > 0:
            return ("new-spend",
                    "$%.2f in the latest week against nothing at all before it. "
                    "This organization started spending inside the window."
                    % latest)
        return ("no-spend", "no spend in any of the %d week(s) read" % len(series))

    oldest_first = list(reversed(series))
    climbing = all(b > a for a, b in zip(oldest_first, oldest_first[1:]))
    if (len(series) >= 4 and climbing and oldest_first[0] > 0
            and (latest - oldest_first[0]) / oldest_first[0] > threshold):
        return ("ramp",
                "every one of %d week(s) is higher than the one before it, "
                "$%.2f to $%.2f (+%.0f%%). A week-over-week check never sees "
                "this, because the growth is already in the baseline."
                % (len(series), oldest_first[0], latest,
                   100 * (latest - oldest_first[0]) / oldest_first[0]))

    change = (latest - baseline) / baseline
    if change > threshold:
        older = series[2:]
        older_baseline = sum(older) / len(older) if older else 0.0
        if older_baseline > 0 and (series[1] - older_baseline) / older_baseline > threshold:
            return ("step",
                    "$%.2f in the latest week and $%.2f in the one before it, "
                    "against a $%.2f baseline before that. The new level has "
                    "held for two weeks, so something shipped rather than ran "
                    "once." % (latest, series[1], older_baseline))
        return ("spike",
                "$%.2f in the latest week against a $%.2f baseline (+%.0f%%), "
                "and the week before it was normal. One week high is a job that "
                "ran, not a level that changed."
                % (latest, baseline, change * 100))
    if change < -threshold:
        return ("drop",
                "$%.2f in the latest week against a $%.2f baseline (%.0f%%). "
                "Spend falling this fast is usually traffic that stopped rather "
                "than money that was saved." % (latest, baseline, change * 100))
    return ("flat",
            "$%.2f against a $%.2f baseline (%+.1f%%)"
            % (latest, baseline, change * 100))


def get(session, url, params, headers=None):
    r = session.get(url, params=params, headers=headers or {}, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from the cost report: this endpoint needs an "
                         "organization admin key, not a project or workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def openai_buckets(session, days, max_pages=40):
    params = {"start_time": int(time.time()) - days * 86400,
              "bucket_width": "1d", "limit": min(180, max(1, days))}
    for _ in range(max_pages):
        page = get(session, OPENAI_API + "/organization/costs", params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params = dict(params)
        params["page"] = page["next_page"]


def anthropic_buckets(session, days, max_pages=40):
    started = datetime.now(timezone.utc) - timedelta(days=days)
    params = {"starting_at": started.strftime("%Y-%m-%dT00:00:00Z"), "limit": 31}
    headers = {"anthropic-version": ANTHROPIC_VERSION}
    for _ in range(max_pages):
        page = get(session, ANTHROPIC_API + "/organizations/cost_report",
                   params, headers)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params = dict(params)
        params["page"] = page["next_page"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", choices=("openai", "anthropic"),
                    default="openai", help="which cost report to read")
    ap.add_argument("--weeks", type=int, default=8,
                    help="whole weeks to read (default 8)")
    ap.add_argument("--threshold", type=float, default=0.40,
                    help="fractional change worth reporting (default 0.40)")
    args = ap.parse_args()

    session = requests.Session()
    if args.provider == "openai":
        key = os.environ.get("OPENAI_ADMIN_KEY")
        if not key:
            log.error("set OPENAI_ADMIN_KEY (an organization admin key, "
                      "read-only scopes are enough)")
            return 2
        session.headers.update({"Authorization": "Bearer " + key})
        buckets = list(openai_buckets(session, args.weeks * 7 + 1))
        daily = daily_from_openai(buckets)
    else:
        key = os.environ.get("ANTHROPIC_ADMIN_KEY")
        if not key:
            log.error("set ANTHROPIC_ADMIN_KEY (an Admin API key, sk-ant-admin)")
            return 2
        session.headers.update({"x-api-key": key})
        buckets = list(anthropic_buckets(session, args.weeks * 7 + 1))
        daily = daily_from_anthropic(buckets)

    today = datetime.now(timezone.utc).date().isoformat()
    series = weeks(daily, today, args.weeks)
    if not series:
        log.info("no whole weeks of cost data in the window")
        return 0

    state, detail = classify([total for _, _, total in series], args.threshold)
    first, last, _ = series[0]
    log.info("%d whole week(s) read, most recent %s..%s", len(series), first, last)
    for week_first, week_last, total in series:
        log.info("  %s..%s  $%.2f", week_first, week_last, total)

    if state in FINDINGS:
        log.warning("%-11s %s..%s  %s", state, first, last, detail)
        log.warning("  repair: attribute the delta before you act on it. Group "
                    "the same window by line item and by project and read the "
                    "rows that moved, rather than the rows you remember being "
                    "expensive.")
        if args.provider == "openai":
            log.warning("  repair: print, do not run. Set a ceiling with "
                        "POST /v1/organization/spend_limit "
                        "{'threshold_amount': <cents>, 'currency': 'USD', "
                        "'interval': 'month'} and an early warning with "
                        "POST /v1/organization/spend_alerts at about 60% of it.")
        else:
            log.warning("  repair: Anthropic has no spend-limit endpoint. Set "
                        "the organization and per-workspace limits in the "
                        "console, and re-read this window first because late "
                        "events revise the recent past.")
        return 1

    log.info("%-11s %s..%s  %s", state, first, last, detail)
    log.info("%d whole week(s) read, no change worth reporting", len(series))
    return 0


if __name__ == "__main__":
    sys.exit(main())
