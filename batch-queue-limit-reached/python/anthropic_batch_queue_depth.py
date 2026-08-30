"""Measure live batch queue depth against the organization's enqueued ceiling.

Read only. Two GET endpoints on two different credentials:
GET /v1/organizations/rate_limits?group_type=batch with an Admin key for the
ceiling, and GET /v1/messages/batches with a workspace key for the depth.
Nothing is submitted and nothing is cancelled.

The Message Batches API has its own limits, shared across all models. The one
this measures is the number of batch requests allowed in the processing queue
at once. A batch request is part of that queue when it has yet to be
successfully processed by the model, which is exactly request_counts.processing.

Scope caveat, printed with every result: the ceiling is organization wide and
the batch list is workspace scoped, so a single workspace key produces a lower
bound. Extra workspace keys tighten it.

This is Anthropic only on purpose. OpenAI's equivalent enqueued-token cap is
not returned by any endpoint, so a read-only script cannot compute the ratio
there at all.
"""
import argparse
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_batch_queue_depth")

RATE_LIMITS_URL = "https://api.anthropic.com/v1/organizations/rate_limits"
BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"

# The two non-terminal processing_status values. A batch that has ended holds
# nothing in the queue whatever its other counts say.
LIVE_STATES = ("in_progress", "canceling")

# Documented at every tier, and the same at every tier, which is why it is safe
# to print as context rather than looked up per organization.
PER_BATCH_REQUESTS = 100000
PER_BATCH_MB = 256

FINDINGS = ("queue-exhausted", "queue-near-limit", "queue-limit-unknown")


def enqueued_limit(payload):
    """The enqueued_batch_requests value, or None. Pure.

    None rather than zero when it is missing. Zero would read as a ceiling of
    nothing and turn every run into a false alarm at infinite occupancy.
    """
    for group in (payload or {}).get("data") or []:
        if not isinstance(group, dict):
            continue
        if group.get("group_type") != "batch":
            continue
        for limit in group.get("limits") or []:
            if isinstance(limit, dict) and limit.get("type") == "enqueued_batch_requests":
                try:
                    return int(limit.get("value"))
                except (TypeError, ValueError):
                    return None
    return None


def queue_rows(batches, workspace=""):
    """Live batches and what each holds in the queue. Pure.

    processing, and only processing. Adding succeeded or errored would count
    requests the model has already finished with, which are not in the queue.
    """
    out = []
    for b in batches or []:
        status = str((b or {}).get("processing_status") or "")
        if status not in LIVE_STATES:
            continue
        counts = b.get("request_counts") or {}
        try:
            processing = int(counts.get("processing") or 0)
        except (TypeError, ValueError):
            processing = 0
        out.append({"id": str(b.get("id")), "status": status,
                    "processing": processing, "workspace": workspace})
    return sorted(out, key=lambda r: (-r["processing"], r["id"]))


def queue_depth(rows):
    """Total requests waiting on the model. Pure."""
    return sum(int(r.get("processing") or 0) for r in rows or [])


def headroom(depth, limit):
    """(remaining, occupancy) or (None, None) when the ceiling is unknown. Pure."""
    if limit is None or limit <= 0:
        return (None, None)
    return (max(0, limit - depth), depth / float(limit))


def top_holders(rows, n=3):
    """The n biggest contributors. Pure. Gives the finding a subject."""
    return [r for r in (rows or [])[:max(0, n)] if int(r.get("processing") or 0) > 0]


def workspace_keys(primary, extra):
    """Deduplicated workspace credentials. Pure. Order kept.

    The same key passed twice would double the measured depth, which is the one
    error in this script that would look like a real finding.
    """
    out, seen = [], set()
    for candidate in [primary] + str(extra or "").split(","):
        key = (candidate or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def verdict(depth, limit, rows, workspaces, threshold):
    """Grade the run. Pure. Returns (state, detail)."""
    remaining, occupancy = headroom(depth, limit)
    if limit is None:
        return ("queue-limit-unknown",
                "%d batch requests are in the processing queue across %d "
                "workspace(s), but the enqueued_batch_requests ceiling could "
                "not be read, so there is no headroom to report"
                % (depth, workspaces))
    percent = int(round(occupancy * 100))
    if depth >= limit:
        return ("queue-exhausted",
                "%d of %d enqueued batch requests are in the processing queue, "
                "which is the whole ceiling. New submissions are being refused"
                % (depth, limit))
    if percent >= threshold:
        return ("queue-near-limit",
                "%d of %d enqueued batch requests are in the processing queue, "
                "which is %d%% of the ceiling" % (depth, limit, percent))
    return ("queue-clear",
            "%d of %d enqueued batch requests are in the processing queue, "
            "leaving %d requests of headroom across %d live batch(es)"
            % (depth, limit, remaining, len(rows or [])))


def repair_lines(state, rows, limit):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state == "queue-clear":
        return ["nothing to change. Keep the check running through the batch "
                "window rather than once a day: this is a queue that drains."]
    if state == "queue-limit-unknown":
        return ["read the ceiling with an Admin key: GET "
                "/v1/organizations/rate_limits?group_type=batch returns "
                "enqueued_batch_requests for the organization. Workspace keys "
                "are rejected by every Admin endpoint.",
                "without the ceiling this run is a raw count. It cannot tell "
                "you whether the next submission will be accepted."]
    lines = ["hold at most a few batches in flight and wait for one to end "
             "before submitting the next. A batch request leaves the queue "
             "only when the model has processed it."]
    biggest = top_holders(rows, 1)
    if biggest and limit:
        lines.append("%s alone holds %d of the %d. Split submissions of that "
                     "size: the per batch cap is %d requests or %d MB, "
                     "whichever comes first."
                     % (biggest[0]["id"], biggest[0]["processing"], limit,
                        PER_BATCH_REQUESTS, PER_BATCH_MB))
    lines.append("a queue held at the ceiling also slows what is already in it, "
                 "and slowed batches are the ones that run out of their 24 hour "
                 "window. Draining is the fix for both.")
    return lines


def get_json(url, headers, params=None, timeout=30):
    """One GET. Returns (payload, error). Read only, always."""
    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
    except requests.RequestException as exc:
        return (None, "request failed: %s" % exc)
    if r.status_code != 200:
        return (None, "HTTP %d %s" % (r.status_code, (r.text or "")[:160]))
    try:
        return (r.json(), None)
    except ValueError:
        return (None, "response was not JSON")


def read_ceiling(admin_key, max_pages=5):
    """The organization's enqueued_batch_requests. Returns (limit, error)."""
    headers = {"x-api-key": admin_key, "anthropic-version": "2023-06-01"}
    params = {"group_type": "batch"}
    for _ in range(max(1, max_pages)):
        payload, err = get_json(RATE_LIMITS_URL, headers, params)
        if err:
            return (None, err)
        found = enqueued_limit(payload)
        if found is not None:
            return (found, None)
        nxt = payload.get("next_page")
        if not nxt:
            return (None, "no batch group in the rate limits response")
        params = {"group_type": "batch", "page": nxt}
    return (None, "the rate limits response never carried a batch group")


def read_batches(key, max_pages=20):
    """One workspace's batches. Returns (rows, error). GETs only."""
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    rows, after = [], None
    for _ in range(max(1, max_pages)):
        params = {"limit": 1000}
        if after:
            params["after_id"] = after
        payload, err = get_json(BATCHES_URL, headers, params)
        if err:
            return (rows, err)
        data = payload.get("data") or []
        rows.extend(data)
        if not payload.get("has_more") or not data:
            break
        after = payload.get("last_id") or data[-1].get("id")
        if not after:
            break
    return (rows, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=int, default=80,
                    help="percent occupancy at which the queue is a finding")
    ap.add_argument("--max-pages", type=int, default=20)
    args = ap.parse_args()

    admin_key = os.environ.get("ANTHROPIC_ADMIN_KEY")
    keys = workspace_keys(os.environ.get("ANTHROPIC_API_KEY"),
                          os.environ.get("ANTHROPIC_EXTRA_WORKSPACE_KEYS"))
    if not keys:
        log.error("set ANTHROPIC_API_KEY to a workspace key. Add "
                  "ANTHROPIC_EXTRA_WORKSPACE_KEYS as a comma separated list to "
                  "cover more of the organization")
        return 2

    limit = None
    if admin_key:
        limit, err = read_ceiling(admin_key)
        if err:
            log.warning("could not read the ceiling: %s", err)
    else:
        log.warning("no ANTHROPIC_ADMIN_KEY, so the enqueued_batch_requests "
                    "ceiling cannot be read and only the raw depth is available")
    if limit is not None:
        log.info("%-12s enqueued_batch_requests %s (organization wide)",
                 "ceiling", format(limit, ","))

    rows = []
    for index, key in enumerate(keys):
        batches, err = read_batches(key, args.max_pages)
        if err:
            log.warning("workspace %d batch list stopped early: %s", index + 1, err)
        rows.extend(queue_rows(batches, workspace="ws%d" % (index + 1)))
    rows.sort(key=lambda r: (-r["processing"], r["id"]))

    for row in rows:
        log.info("%-16s %-13s %s processing", row["id"][:16], row["status"],
                 format(row["processing"], ","))

    depth = queue_depth(rows)
    state, detail = verdict(depth, limit, rows, len(keys), args.threshold)
    emit = log.warning if state in FINDINGS else log.info
    emit("%-20s %s", state, detail)
    emit("  measured: enqueued_batch_requests from the Rate Limits API, and the "
         "sum of request_counts.processing over %d live batch(es) in %d "
         "workspace(s)", len(rows), len(keys))
    emit("  inferred: nothing about workspaces whose keys were not supplied. "
         "The ceiling is organization wide and this depth is a lower bound on it")
    for line in repair_lines(state, rows, limit):
        emit("  repair: %s", line)

    log.info("%d finding(s)", 1 if state in FINDINGS else 0)
    return 1 if state in FINDINGS else 0


if __name__ == "__main__":
    sys.exit(main())
