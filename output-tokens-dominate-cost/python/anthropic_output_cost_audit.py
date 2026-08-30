"""Report which side of a Claude request the bill is actually on.

Read only. Two GET requests and nothing else: ANTHROPIC_ADMIN_KEY must be an
Admin API key (sk-ant-admin...), because every /v1/organizations endpoint
rejects a workspace key. The repair is printed, never performed.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_output_cost_audit")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# Output is priced at five times input on every current model, so a request has
# to be markedly input-heavy before input is the larger line.
OUTPUT_MULTIPLE = 5


def amount(row):
    """Read a cost row's amount as a float. Pure.

    The cost report returns amount as a decimal STRING, not a number. Summing
    the raw values concatenates them in one language and throws in the other,
    and the failure is silent enough to ship.
    """
    raw = row.get("amount")
    if raw is None or raw == "":
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def bucket_of(token_type):
    """Fold a token_type into one of five buckets. Pure.

    Matched on the shape of the name rather than an exact list, because new
    token types arrive with new cache durations and new tiers. Anything
    unrecognised lands in "other" and stays visible; a silently dropped type is
    a set of shares that quietly adds up to less than one.
    """
    name = str(token_type or "").lower()
    if not name:
        return "other"
    if "cache_creation" in name or "cache_write" in name:
        return "cache_write"
    if "cache_read" in name:
        return "cache_read"
    if "output" in name:
        return "output"
    if "input" in name:
        return "input"
    return "other"


def by_bucket(cost_buckets):
    """Sum spend per token bucket across the cost report. Pure."""
    out = {"input": 0.0, "output": 0.0, "cache_read": 0.0,
           "cache_write": 0.0, "other": 0.0}
    for b in cost_buckets:
        for r in b.get("results", []) or []:
            out[bucket_of(r.get("token_type"))] += amount(r)
    return out


def top_model(usage_buckets):
    """The model carrying the most output tokens, and its share. Pure.

    Returns (model, share) or (None, 0.0). Answers the only actionable question
    the usage report can answer here: where an effort change would land.
    """
    per_model = {}
    total = 0
    for b in usage_buckets:
        for r in b.get("results", []) or []:
            model = r.get("model") or "unspecified"
            out = int(r.get("output_tokens") or 0)
            per_model[model] = per_model.get(model, 0) + out
            total += out
    if not total:
        return (None, 0.0)
    model = max(per_model, key=lambda m: per_model[m])
    return (model, per_model[model] / total)


def verdict(buckets, min_spend=1.0):
    """Turn the spend split into the lever that will actually move it. Pure.

    Returns (state, detail). The states are not degrees of the same finding:
    each one names a different repair, and applying the wrong one costs a week
    and changes nothing on the invoice.
    """
    total = sum(buckets.values())
    if total < min_spend:
        return ("no-spend", "$%.2f over the window: nothing to act on" % total)

    def pct(key):
        return buckets[key] / total * 100

    split = ("output %.0f%%, input %.0f%%, cache read %.0f%%, cache write %.0f%%"
             % (pct("output"), pct("input"), pct("cache_read"), pct("cache_write")))
    if buckets["other"] > 0:
        split += ", unrecognised %.0f%%" % pct("other")
    money = "$%.2f over the window: %s" % (total, split)

    if buckets["cache_write"] > buckets["cache_read"] and pct("cache_write") >= 15:
        return ("cache-write-heavy",
                "%s. You are paying the cache write premium without the reads "
                "to amortise it: the prefix is being rewritten more often than "
                "it is hit." % money)

    if pct("output") >= 70:
        return ("output-dominated",
                "%s. Output is priced at %dx input and there is no caching "
                "discount on it, so the only lever is generating fewer tokens: "
                "lower effort, tighter stop conditions, shorter output formats."
                % (money, OUTPUT_MULTIPLE))

    if pct("input") + pct("cache_read") + pct("cache_write") >= 60:
        return ("input-dominated",
                "%s. This is the shape prompt caching is for. Cache the stable "
                "prefix and read it back; trimming output here buys very "
                "little." % money)

    if pct("output") >= 50:
        return ("output-led",
                "%s. Output is the larger half but not overwhelmingly. Both "
                "levers help and neither is dramatic on its own." % money)

    return ("balanced", money)


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations endpoints need an "
                         "Admin API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def read_all(session, path, params):
    """Follow next_page until the report is exhausted."""
    out = []
    while True:
        page = get(session, path, params)
        out.extend(page.get("data", []))
        if not page.get("has_more") or not page.get("next_page"):
            break
        params = [p for p in params if p[0] != "page"] + [("page", page["next_page"])]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="how far back to read the cost and usage reports")
    ap.add_argument("--min-spend", type=float, default=1.0,
                    help="below this total, report nothing rather than a noisy share")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not key:
        log.error("set ANTHROPIC_ADMIN_KEY (an Admin API key, sk-ant-admin...; "
                  "workspace keys are rejected by /v1/organizations/*)")
        return 2

    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=args.days)).strftime("%Y-%m-%dT00:00:00Z")

    s = requests.Session()
    s.headers.update({"x-api-key": key, "anthropic-version": VERSION})

    costs = read_all(s, "/organizations/cost_report",
                     [("starting_at", since), ("limit", 31),
                      ("group_by[]", "description")])
    usage = read_all(s, "/organizations/usage_report/messages",
                     [("starting_at", since), ("bucket_width", "1d"),
                      ("limit", 31), ("group_by[]", "model")])

    split = by_bucket(costs)
    state, detail = verdict(split, args.min_spend)
    line = "%-18s %s" % (state, detail)

    bad = 0
    if state in ("no-spend", "balanced", "input-dominated"):
        log.info(line)
    else:
        bad = 1
        log.warning(line)

    model, share = top_model(usage)
    if model:
        log.info("top model by output tokens: %s (%.0f%% of output)",
                 model, share * 100)
        if bad:
            log.warning("  repair, to run yourself: lower output_config.effort on "
                        "%s (high to medium is the usual first step), then re-read "
                        "this same daily series a week later. Thinking tokens bill "
                        "as output, so effort is the setting that moves this share.",
                        model)
            log.warning("  never change an effort setting from inside an audit; "
                        "the Admin API cannot do it and neither should this.")
    else:
        log.info("no output tokens in the usage report for this window")

    log.info("%d cost bucket(s), %d usage bucket(s) over %d day(s), %d finding(s)",
             len(costs), len(usage), args.days, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
