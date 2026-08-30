"""Find flex tier work that was never served, and flex you never actually asked for.

Read only. One paged GET with an admin key:

  GET /v1/organization/usage/completions
      ?bucket_width=1h&group_by[]=service_tier&group_by[]=model

The tier on each result is the tier the request was actually served on, which is
what makes this readable at all. Two opposite findings come out of it: a model
configured for flex with no flex rows anywhere (the parameter is not arriving),
and hours where flex volume collapses while other tiers keep serving (capacity
was refused).

A 429 Resource Unavailable is explicitly not charged, so it never appears in any
usage report. The evidence for it is a hole, which is one inference further from
the data than everything else in this section, so the gap test is deliberately
conservative: below half the median served hour, in an hour the organization was
otherwise active, with enough served hours to have a median worth comparing to.

The cost report cannot substitute: its group_by accepts project_id, line_item
and api_key_id and has no service tier dimension at all.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_flex_tier_served")

API = "https://api.openai.com/v1"
FLEX = "flex"

# Enough served hours to have a median worth comparing against. Below this the
# script says it cannot tell rather than grading two data points.
MIN_SERVED_HOURS = 6

FINDINGS = ("flex-never-served", "flex-shortfall")


def num(value):
    """A float, or 0.0. Pure."""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def tier_rows(pages):
    """{(model, tier): {hour: {"requests", "input", "output"}}}. Pure.

    Hours with no row for a pairing are simply absent. Filling them with zeros
    here would destroy the difference between "served nothing" and "was not
    grouped this way", which is the whole subject.
    """
    out = {}
    for page in pages or []:
        for bucket in ((page or {}).get("data") or []):
            hour = int(num((bucket or {}).get("start_time")))
            for result in ((bucket or {}).get("results") or []):
                result = result or {}
                key = (str(result.get("model") or "(all models)"),
                       str(result.get("service_tier") or "(untiered)"))
                row = out.setdefault(key, {}).setdefault(
                    hour, {"requests": 0.0, "input": 0.0, "output": 0.0})
                row["requests"] += num(result.get("num_model_requests"))
                row["input"] += num(result.get("input_tokens"))
                row["output"] += num(result.get("output_tokens"))
    return out


def totals_by_tier(rows):
    """{tier: total requests}. Pure."""
    out = {}
    for (_model, tier), hours in (rows or {}).items():
        out[tier] = out.get(tier, 0.0) + sum(h["requests"] for h in hours.values())
    return out


def hours_active(rows):
    """{hour: requests served across every tier}. Pure.

    The control. Without it a night when the job did not run reads exactly like
    a night when flex refused every request.
    """
    out = {}
    for hours in (rows or {}).values():
        for hour, counts in hours.items():
            out[hour] = out.get(hour, 0.0) + counts["requests"]
    return out


def median(values):
    """The median of a list. Pure. 0.0 when empty.

    Median rather than mean on purpose: one enormous backfill hour drags a mean
    high enough to swallow the very gaps this is looking for.
    """
    ordered = sorted(float(v) for v in (values or []))
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def flex_by_hour(rows, model):
    """{hour: flex requests} for one model. Pure."""
    return {hour: counts["requests"]
            for hour, counts in ((rows or {}).get((model, FLEX)) or {}).items()}


def tiers_for_model(rows, model):
    """{tier: requests} for one model across every tier. Pure."""
    out = {}
    for (candidate, tier), hours in (rows or {}).items():
        if candidate != model:
            continue
        out[tier] = out.get(tier, 0.0) + sum(h["requests"] for h in hours.values())
    return out


def flex_gaps(flex_hours, active, floor=0.5, min_served=MIN_SERVED_HOURS):
    """[(hour, flex_requests, other_requests, median)] where flex collapsed. Pure.

    Three guards, all of them there to stop absence being over-read. The hour
    must be at or below floor times the median served hour; some tier must have
    served something in that same hour; and there must be at least min_served
    hours of flex traffic to take a median from at all.
    """
    served = [v for v in (flex_hours or {}).values() if v > 0]
    if len(served) < min_served:
        return []
    mid = median(served)
    if mid <= 0:
        return []
    out = []
    for hour, total in sorted((active or {}).items()):
        flex = float((flex_hours or {}).get(hour, 0.0))
        other = float(total) - flex
        if flex <= mid * floor and other > 0:
            out.append((hour, flex, other, mid))
    out.sort(key=lambda r: (r[1], r[0]))
    return out


def never_served(rows, configured):
    """[(model, flex_requests, {tier: requests})] for models with no flex rows.

    Pure. configured is the list of model ids your code sends flex for, because
    nothing the API returns knows what you meant to ask for. Without it this
    check cannot run, and guessing would report every deliberately standard
    model as a fault.
    """
    out = []
    for model in sorted(set(str(m) for m in (configured or []) if m)):
        tiers = tiers_for_model(rows, model)
        if tiers.get(FLEX, 0.0) > 0:
            continue
        if sum(tiers.values()) <= 0:
            continue
        out.append((model, 0.0, tiers))
    return out


def verdict(model, flex_hours, gaps, tiers, configured):
    """Classify one model. Pure. Returns (state, detail)."""
    tiers = tiers or {}
    flex_total = tiers.get(FLEX, 0.0)
    other_total = sum(v for t, v in tiers.items() if t != FLEX)
    if flex_total <= 0 and model in set(configured or []):
        if other_total <= 0:
            return ("no-usage", "no requests on any tier in this window")
        return ("flex-never-served",
                "%s flex request(s) in this window, and %s on other tiers. The "
                "service_tier parameter is not reaching the API."
                % (fmt(flex_total), fmt(other_total)))
    if flex_total <= 0:
        return ("no-flex-usage", "never served on flex in this window")
    if gaps:
        mid = gaps[0][3]
        return ("flex-shortfall",
                "%d hour(s) at or below half the median served hour (median %s "
                "requests)" % (len(gaps), fmt(mid)))
    served = len([v for v in (flex_hours or {}).values() if v > 0])
    if served < MIN_SERVED_HOURS:
        return ("too-little-history",
                "%d hour(s) of flex traffic, which is not enough to take a "
                "median from" % served)
    return ("flex-served",
            "%s flex request(s) across %d hour(s), no collapsed hours"
            % (fmt(flex_total), served))


def fmt(value):
    """Thousands separators. Pure."""
    return "{:,}".format(int(round(num(value))))


def stamp(hour):
    """An hour bucket's start as a readable UTC string. Pure."""
    return dt.datetime.fromtimestamp(int(hour), dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:00Z")


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state == "flex-never-served":
        return ["the tier in this report is the tier that was served. Check for a "
                "gateway that rewrites request bodies, an SDK wrapper with its "
                "own defaults, or a code path that never set service_tier at all.",
                "until it arrives you are paying standard rates for a workload "
                "you believe is discounted, and nothing will raise about it."]
    if state == "flex-shortfall":
        return ["back off and retry on 429 Resource Unavailable, which means no "
                "capacity right now rather than a limit you exceeded. Retrying "
                "it genuinely helps, unlike the billing 429s.",
                "raise the client timeout to at least 15 minutes. The official "
                "SDK default is 10 and flex responses regularly exceed it, and "
                "an aborted request can still be billed if the server finishes.",
                "fall back to service_tier auto when completing the work matters "
                "more than the discount, and keep flex off anything a person is "
                "waiting for."]
    if state == "too-little-history":
        return ["not a clean bill of health, just too little to read. Re-run over "
                "a longer window once the job has more served hours behind it."]
    return []


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: the organization usage endpoints need "
                         "an admin key" % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, **params):
    params = dict(params)
    for _ in range(50):
        page = get(session, path, **params)
        yield page
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--flex-model", action="append", default=[],
                    help="a model id your code sends service_tier flex for "
                         "(repeatable)")
    ap.add_argument("--days", type=float, default=7.0,
                    help="window in days (max 7 at hourly buckets)")
    ap.add_argument("--floor", type=float, default=0.5,
                    help="share of the median served hour below which an hour "
                         "counts as collapsed")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_ADMIN_KEY")
    if not key:
        log.error("set OPENAI_ADMIN_KEY to an admin key that can read the "
                  "organization usage endpoints")
        return 2
    days = max(0.5, min(7.0, args.days))

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + key,
                      "User-Agent": "openai-flex-tier-served/1.0"})

    now = dt.datetime.now(dt.timezone.utc)
    start = int((now - dt.timedelta(days=days)).timestamp())
    payloads = list(pages(s, "/organization/usage/completions",
                          start_time=start, bucket_width="1h", limit=168,
                          **{"group_by[]": ["service_tier", "model"]}))

    rows = tier_rows(payloads)
    totals = totals_by_tier(rows)
    active = hours_active(rows)
    buckets = sum(len(page.get("data") or []) for page in payloads)
    log.info("%d hourly bucket(s), %d tier(s) observed: %s",
             buckets, len(totals), ", ".join(sorted(totals)) or "none")

    configured = [str(m) for m in args.flex_model if m]
    if not configured:
        log.info("no --flex-model given, so the never-served check is skipped: "
                 "nothing the API returns knows which models your code asks for "
                 "flex on")

    findings = 0
    models = sorted({model for model, _tier in rows} | set(configured))
    for model in models:
        flex_hours = flex_by_hour(rows, model)
        gaps = flex_gaps(flex_hours, active, args.floor)
        tiers = tiers_for_model(rows, model)
        state, detail = verdict(model, flex_hours, gaps, tiers, configured)
        if state in ("no-flex-usage", "no-usage") and model not in configured:
            continue
        emit = log.warning if state in FINDINGS else log.info
        emit("%-21s %s: %s", state, model, detail)
        for hour, flex, other, _mid in gaps[:5]:
            emit("  %s  %s requests, other tiers served %s that hour",
                 stamp(hour), fmt(flex), fmt(other))
        if state == "flex-shortfall":
            emit("  note: a 429 Resource Unavailable is not charged and never "
                 "reaches this report, so these hours are absence rather than "
                 "error counts.")
        for line in repair_lines(state):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    for model, _flex, tiers in never_served(rows, configured):
        if model in models:
            continue
        log.warning("%-21s %s: served only on %s", "flex-never-served", model,
                    ", ".join("%s (%s)" % (t, fmt(v))
                              for t, v in sorted(tiers.items())))
        findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
