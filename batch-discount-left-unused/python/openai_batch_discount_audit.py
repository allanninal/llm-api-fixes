"""Report synchronous OpenAI traffic that is shaped like batch work.

Read only. Two GET requests against the organization endpoints and nothing
else. Those endpoints reject project keys, so this needs an organization admin
key (sk-admin-), which can and should be provisioned read-only.

This is a cost note, not a failure note. Nothing found here is broken: the
finding is latency-insensitive work paying interactive prices, and the repair
is a change to how a job submits its requests, printed for you to run.
"""
import argparse
import logging
import math
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_batch_discount_audit")

API = "https://api.openai.com/v1"

# The Batch API is priced at half the synchronous rate on both input and output
# tokens, in exchange for a completion window of up to 24 hours.
DISCOUNT = 0.50


def accumulate(buckets):
    """Fold usage buckets into one row per project and model. Pure.

    The hourly request counts have to stay aligned across the whole window, so
    each row carries a list as long as the bucket list with zeros where that
    workload was idle. Compacting out the idle hours would make every workload
    look concentrated, which is exactly the thing being measured.
    """
    buckets = list(buckets or [])
    rows = {}
    for index, bucket in enumerate(buckets):
        for result in bucket.get("results") or []:
            project = str(result.get("project_id") or "unknown")
            model = str(result.get("model") or "unknown")
            key = "%s / %s" % (project, model)
            row = rows.get(key)
            if row is None:
                row = {"key": key, "project_id": project, "model": model,
                       "sync_requests": 0, "batch_requests": 0,
                       "sync_input": 0, "sync_output": 0,
                       "hourly": [0] * len(buckets)}
                rows[key] = row
            requests_made = int(result.get("num_model_requests") or 0)
            if result.get("batch") is True:
                row["batch_requests"] += requests_made
            else:
                row["sync_requests"] += requests_made
                row["sync_input"] += int(result.get("input_tokens") or 0)
                row["sync_output"] += int(result.get("output_tokens") or 0)
                row["hourly"][index] += requests_made
    return rows


def concentration(hourly, top_fraction=0.10):
    """Share of requests inside the busiest slice of the window. Pure.

    Returns a float between 0 and 1, or None when there is nothing to measure.
    A scheduled job puts most of its week into a handful of hours; an audience
    does not, however uneven its day looks.
    """
    counts = [int(c or 0) for c in (hourly or [])]
    total = sum(counts)
    if not counts or total <= 0:
        return None
    top = max(1, int(math.ceil(len(counts) * top_fraction)))
    return sum(sorted(counts, reverse=True)[:top]) / float(total)


def verdict(row, min_requests=1000, threshold=0.70, top_fraction=0.10):
    """Classify one workload's week. Pure. Returns (state, detail).

    "interactive" and "already-batched" are answers, not failures to detect
    something: synchronous is the correct endpoint for traffic with a person
    waiting on it, and this script says so rather than staying silent.
    """
    sync = int(row.get("sync_requests") or 0)
    batched = int(row.get("batch_requests") or 0)
    total = sync + batched

    if total < min_requests:
        return ("too-little-traffic",
                "%d request(s) in the window, which is too few to say anything "
                "about the shape" % total)

    share = sync / float(total)
    if share < 0.20:
        return ("already-batched",
                "%.0f%% of %d request(s) already go through the Batch API"
                % (100 * (1 - share), total))

    spike = concentration(row.get("hourly"), top_fraction)
    if spike is None:
        return ("unmeasurable",
                "%d synchronous request(s) and no per bucket counts to spread "
                "them over, so the shape cannot be measured" % sync)

    if spike >= threshold:
        return ("batch-shaped",
                "%.0f%% of %d synchronous request(s) land in the busiest %.0f%% "
                "of hours. That is a schedule, not an audience, and it is paying "
                "interactive prices." % (spike * 100, sync, top_fraction * 100))
    return ("interactive",
            "%d synchronous request(s), %.0f%% of them in the busiest %.0f%% of "
            "hours. Spread out like traffic with someone waiting on it, so the "
            "synchronous endpoint is the right one."
            % (sync, spike * 100, top_fraction * 100))


def sync_cost(buckets, project_id=None):
    """Non-batch dollars in the cost report, optionally for one project. Pure.

    Batch and non-batch appear as distinct line_item strings, so the split is a
    substring test and nothing more clever than that. Reading the money from the
    cost report rather than from a per-token price table is deliberate: the
    table goes stale, the report does not.
    """
    total = 0.0
    for bucket in buckets or []:
        for result in bucket.get("results") or []:
            if project_id and str(result.get("project_id") or "") != project_id:
                continue
            if "batch" in str(result.get("line_item") or "").lower():
                continue
            try:
                total += float((result.get("amount") or {}).get("value") or 0.0)
            except (TypeError, ValueError):
                continue
    return round(total, 2)


def saving(sync_cost_usd, discount=DISCOUNT):
    """What the same spend would have been worth at batch prices. Pure.

    Not a promise: it is the value of the discount on money already spent, and
    it says nothing about whether the job can accept a 24 hour window. That
    part is a fact about your schedule and no endpoint knows it.
    """
    if sync_cost_usd is None:
        return None
    try:
        return round(max(0.0, float(sync_cost_usd)) * discount, 2)
    except (TypeError, ValueError):
        return None


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an "
                         "organization admin key (sk-admin-), not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params, max_pages=40):
    """Walk a usage or cost report, which paginates on an opaque page cursor."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, path, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params = dict(params)
        params["page"] = page["next_page"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7,
                    help="days of hourly buckets to read (default 7)")
    ap.add_argument("--min-requests", type=int, default=1000,
                    help="ignore workloads below this many requests (default 1000)")
    ap.add_argument("--threshold", type=float, default=0.70,
                    help="share of requests in the busiest hours above which a "
                         "workload is called batch shaped (default 0.70)")
    ap.add_argument("--top-fraction", type=float, default=0.10,
                    help="the busiest share of buckets to measure against "
                         "(default 0.10)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print workloads that are correctly synchronous")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_ADMIN_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key, read-only "
                  "scopes are enough)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    start = int(time.time()) - args.days * 86400
    usage = list(pages(session, "/organization/usage/completions", {
        "start_time": start,
        "bucket_width": "1h",
        "limit": 168,
        "group_by": ["batch", "project_id", "model"],
    }))
    costs = list(pages(session, "/organization/costs", {
        "start_time": start,
        "bucket_width": "1d",
        "limit": 31,
        "group_by": ["line_item", "project_id"],
    }))

    rows = accumulate(usage)
    if not rows:
        log.info("no completions usage in the last %d day(s) for this "
                 "organization", args.days)
        return 0

    found = 0
    for key_name in sorted(rows):
        row = rows[key_name]
        state, detail = verdict(row, args.min_requests, args.threshold,
                                args.top_fraction)
        line = "%-17s %s  %s" % (state, key_name, detail)
        if state == "batch-shaped":
            found += 1
            log.warning(line)
            spend = sync_cost(costs, row["project_id"])
            worth = saving(spend)
            log.warning("  cost: $%.2f of synchronous spend on project %s over "
                        "%d day(s); about $%.2f of that is the batch discount "
                        "you are not taking", spend, row["project_id"],
                        args.days, worth)
            log.warning("  repair: upload the requests as a .jsonl to /v1/files "
                        "with purpose=batch, create a batch with a 24h "
                        "completion window, and read both result files. The "
                        "trade is half price for no latency guarantee.")
        elif state in ("interactive", "already-batched", "too-little-traffic"):
            if args.show_all:
                log.info(line)
        else:
            log.warning(line)

    log.info("%d workload(s), %d batch shaped", len(rows), found)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
