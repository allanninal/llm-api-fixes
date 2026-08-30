"""Tell an OpenAI billing wall apart from a real rate limit, before it stops you.

Read only. GET requests and nothing else: OPENAI_ADMIN_KEY is an organization
admin key (sk-admin-...) with read scopes, and OPENAI_API_KEY is an optional
project key set to Read Only, used only for a live probe. The repair is printed,
never performed, because this script holds credentials that can spend money on
inference.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_quota_wall_audit")

API = "https://api.openai.com/v1"

# 429 codes that describe money rather than traffic. None of them clears on
# retry, and each one has a different remedy in a different console.
WALL = {
    "insufficient_quota":
        "no usable balance. This is the older name for the same wall and is "
        "still what many accounts return; add credits or enable auto-recharge.",
    "credit_balance_exhausted":
        "prepaid credits are gone. Add credits or enable auto-recharge.",
    "organization_spend_limit_exceeded":
        "the monthly spend limit you set on the organization was reached. "
        "Raise it, or wait for the interval to reset.",
    "project_spend_limit_exceeded":
        "the spend limit set on this project was reached. Raise it on the "
        "project, not on the organization.",
    "organization_usage_limit_exceeded":
        "the ceiling OpenAI assigns your usage tier was reached. Nothing you "
        "own can raise this; request an increase from OpenAI.",
}

# 429 codes that really are traffic shaping and really do clear on their own.
THROTTLE = ("rate_limit_exceeded", "requests_limit_reached", "tokens_limit_reached")

# The monthly usage limit OpenAI assigns each tier, in dollars.
TIER_LIMIT = {1: 100.0, 2: 500.0, 3: 1000.0, 4: 5000.0, 5: 200000.0}


def error_fields(body):
    """Return (code, type, message) from either provider's error envelope.

    OpenAI nests the useful part under "error"; some proxies and most logged
    exception dumps hand back the inner object on its own. Everything comes
    back as a string, empty when absent, so a caller never has to guard three
    levels of dict access before it can make a decision.
    """
    if not isinstance(body, dict):
        return ("", "", "")
    err = body.get("error")
    if not isinstance(err, dict):
        err = body
    return (str(err.get("code") or ""),
            str(err.get("type") or ""),
            str(err.get("message") or ""))


def classify(status, body):
    """Decide whether an error may be retried. Pure, so the rule is testable
    offline and can be lifted straight into a retry wrapper.

    Returns (state, detail). Only "throttle" and "transient" are safe to retry.
    """
    code, etype, message = error_fields(body)
    low = message.lower()

    if status == 429:
        if code in WALL:
            return ("wall",
                    "%s: %s Retrying cannot clear this, and the SDK still "
                    "raises RateLimitError for it." % (code, WALL[code]))
        if code in THROTTLE:
            return ("throttle",
                    "%s: a real limit on how fast you may send. Back off and "
                    "honour Retry-After." % code)
        if not code:
            if etype == "rate_limit_error":
                return ("throttle",
                        "Anthropic 429 rate_limit_error. It carries no code "
                        "field, so match on type here rather than on code.")
            return ("unclassified-429",
                    "429 with no code and no recognised type. Retry once, then "
                    "fail loudly: an unbounded loop against a wall is worse "
                    "than a page.")
        return ("unclassified-429",
                "429 with unrecognised code %s. Treat as not retryable until "
                "somebody has read it." % code)

    if status == 400 and "credit balance" in low:
        return ("wall",
                "Anthropic reports an exhausted balance as a 400 "
                "invalid_request_error, not a 429. There is no code field to "
                "branch on, so the message is the only signal available; it is "
                "a fragile match and worth an alert of its own when it fires.")

    if status in (401, 403):
        return ("auth",
                "status %d: the key is wrong, revoked, or scoped away from "
                "this endpoint. Retrying will not mint a new one." % status)

    if status >= 500 or status == 408:
        return ("transient", "status %d: server side. Retry with backoff." % status)

    return ("other", "status %d, code %s" % (status, code or "none"))


def headroom(spent, limit):
    """Compare month-to-date spend against a tier ceiling. Pure.

    Returns (state, detail). A missing limit is reported as unknown rather than
    as safe, because the tier is not readable from the API and has to be told
    to the script.
    """
    if limit is None:
        return ("tier-unknown",
                "$%.2f spent this month. Pass --tier to compare it against the "
                "ceiling OpenAI assigns that tier; the API does not expose "
                "which tier you are on." % spent)
    if spent >= limit:
        return ("at-ceiling",
                "$%.2f of a $%.2f monthly ceiling. Inference is returning, or "
                "is about to return, 429 organization_usage_limit_exceeded."
                % (spent, limit))
    if spent >= limit * 0.8:
        return ("approaching",
                "$%.2f of a $%.2f monthly ceiling (%.0f%%). This is the one "
                "wall you can forecast to the day."
                % (spent, limit, spent / limit * 100))
    return ("clear", "$%.2f of a $%.2f monthly ceiling" % (spent, limit))


def stalled(buckets, now, quiet_hours=6.0):
    """Find a cliff in the aggregate usage buckets. Pure, clock passed in.

    Neither provider exposes a per-request log, so a wall that has already been
    hit is not visible as an error rate. It is visible as traffic that stops:
    the most recent bucket carrying num_model_requests, aged against now.

    A bucket with requests but no output tokens is a separate finding -- calls
    that failed before generation -- and is reported as such rather than folded
    into the cliff, because the two have completely different repairs.

    Returns (state, detail).
    """
    rows = []
    for b in buckets:
        start = b.get("start_time")
        reqs = 0
        out = 0
        for r in b.get("results", []) or []:
            reqs += int(r.get("num_model_requests") or 0)
            out += int(r.get("output_tokens") or 0)
        if isinstance(start, (int, float)):
            rows.append((float(start), reqs, out))
    rows.sort()

    if not rows:
        return ("no-data", "no usage buckets returned for this window")

    busy = [r for r in rows if r[1] > 0]
    if not busy:
        return ("no-data",
                "%d bucket(s), none with a single model request. Either nothing "
                "ran, or the wall predates the window." % len(rows))

    barren = [r for r in busy if r[2] == 0]
    if barren:
        return ("failing-before-generation",
                "%d bucket(s) with requests but zero output tokens. Those calls "
                "did not generate: they were rejected before the model ran. "
                "That is an error shape, not a spend shape." % len(barren))

    age = (now.timestamp() - busy[-1][0]) / 3600.0
    if age >= quiet_hours:
        return ("cliff",
                "last model request %.1f hour(s) ago and nothing since. Traffic "
                "stopping dead mid-cycle is what a billing wall looks like from "
                "the usage API, because there is no error log to read." % age)
    return ("flowing", "traffic in the last %.1f hour(s)" % age)


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=30)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization endpoints need an "
                         "organization admin key, not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def probe(key):
    """Make the cheapest real call there is and read what comes back.

    Rate-limit headroom is only ever attached to a response; there is no GET
    that returns it. GET /v1/models does not consume inference quota, so it
    usually answers 200 even while inference is walled off. Treat it as proof
    the key still authenticates, not as proof the account can generate.
    """
    r = requests.get(API + "/models",
                     headers={"Authorization": "Bearer " + key}, timeout=30)
    try:
        body = r.json()
    except ValueError:
        body = {}
    limits = {k: v for k, v in r.headers.items()
              if k.lower().startswith("x-ratelimit")}
    return r.status_code, body, limits


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", type=int, default=0, choices=[0, 1, 2, 3, 4, 5],
                    help="usage tier, for the monthly ceiling comparison (0 = unknown)")
    ap.add_argument("--hours", type=int, default=48,
                    help="how far back to read hourly usage buckets")
    ap.add_argument("--quiet-hours", type=float, default=6.0,
                    help="hours without a model request before it counts as a cliff")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key with read "
                  "scopes; project keys are rejected by /v1/organization/*)")
        return 2

    now = dt.datetime.now(dt.timezone.utc)
    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    costs = get(s, "/organization/costs", start_time=int(month_start.timestamp()),
                bucket_width="1d", limit=31)
    spent = 0.0
    for b in costs.get("data", []):
        for r in b.get("results", []) or []:
            spent += float((r.get("amount") or {}).get("value") or 0.0)

    bad = 0
    state, detail = headroom(spent, TIER_LIMIT.get(args.tier))
    if state in ("clear", "tier-unknown"):
        log.info("%-13s %s", state, detail)
    else:
        bad += 1
        log.warning("%-13s %s", state, detail)
        log.warning("  repair: add prepaid credits, raise the org or project "
                    "spend limit, or ask OpenAI for a higher approved usage "
                    "limit. Which one depends on the error code, not the status.")

    since = now - dt.timedelta(hours=args.hours)
    usage = get(s, "/organization/usage/completions",
                start_time=int(since.timestamp()), bucket_width="1h",
                limit=max(args.hours, 1))
    buckets = usage.get("data", [])
    state, detail = stalled(buckets, now, args.quiet_hours)
    if state == "flowing":
        log.info("%-13s %s", state, detail)
    else:
        bad += 1
        log.warning("%-13s %s", state, detail)

    key = os.environ.get("OPENAI_API_KEY")
    if key:
        status, body, limits = probe(key)
        pstate, pdetail = classify(status, body) if status >= 400 else ("ok", "200")
        if pstate == "ok":
            log.info("probe         GET /v1/models answered 200; headroom %s",
                     limits or "not reported on this response")
        else:
            bad += 1
            log.warning("probe         %s  %s", pstate, pdetail)
    else:
        log.info("probe         skipped: set OPENAI_API_KEY (Read Only) to read "
                 "rate-limit headers from a live response")

    log.info("%d bucket(s) read over %d hour(s), %d finding(s)",
             len(buckets), args.hours, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
