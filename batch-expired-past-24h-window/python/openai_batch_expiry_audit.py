"""Report OpenAI batches that expired, and the ones about to.

Read only. GET requests and nothing else: give this a project key set to Read
Only. The repair is printed, never performed, because re-submitting the rows
that never ran means spending money on inference.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_batch_expiry_audit")

API = "https://api.openai.com/v1"

# completion_window accepts one value. This is not a default, it is the value.
WINDOW = 86400

IN_FLIGHT = ("validating", "in_progress", "finalizing", "cancelling")

# Terminal and not this note: a completed batch finished, a failed one never
# started, a cancelled one was stopped on purpose.
SETTLED = ("completed", "failed", "cancelled")

FINDINGS = ("expired", "overdue", "expiring-soon")


def counts_of(batch):
    """Read request_counts into (total, completed), or None. Pure."""
    counts = batch.get("request_counts")
    if not isinstance(counts, dict):
        return None
    try:
        return (int(counts.get("total") or 0), int(counts.get("completed") or 0))
    except (TypeError, ValueError):
        return None


def deadline(batch):
    """When this batch's window closes, and where the number came from. Pure.

    Returns (unix_seconds, source) or (None, reason). Three timestamps can
    answer this and they are not equally good, which is why the source is
    returned alongside the number rather than thrown away:

      expires_at      the API's own answer. Use it whenever it is there.
      in_progress_at  the window runs from when processing started, so this
                      plus 24h is the deadline whenever expires_at is absent.
      created_at      an upper bound only. Time spent in validating is not part
                      of the window, so this over-estimates the time left.
    """
    for field, offset, source in (
            ("expires_at", 0, "expires_at"),
            ("in_progress_at", WINDOW, "in_progress_at plus 24h"),
            ("created_at", WINDOW,
             "created_at plus 24h, an upper bound: the window starts when the "
             "batch starts processing, not when it was created")):
        raw = batch.get(field)
        if raw in (None, ""):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return (value + offset, source)
    return (None, "no usable timestamp on this object")


def verdict(batch, now, warn_hours=4):
    """Classify one object from GET /v1/batches against a clock you pass in.

    Pure. warn_hours is the headroom below which an in-flight batch is called
    out: 4 hours left of a 24 hour window is the 20 hour mark. Returns
    (state, detail).
    """
    status = str(batch.get("status") or "").strip().lower()
    numbers = counts_of(batch)
    total, done = numbers if numbers else (0, 0)
    rows = ("%d of %d row(s)" % (done, total)) if total else "an unreadable count of rows"

    if status == "expired":
        missing = max(0, total - done)
        return ("expired",
                "the 24 hour window closed with %d row(s) unfinished (%s done). "
                "Each one is a batch_expired line in the error file, and none of "
                "them will run." % (missing, rows))
    if status in SETTLED:
        return ("settled",
                "status is %s, so no window is running against it" % status)
    if status not in IN_FLIGHT:
        return ("unreadable",
                "status is %r, which is not a lifecycle state this script "
                "recognises" % (status or None,))

    when, source = deadline(batch)
    if when is None:
        return ("unreadable",
                "still %s and there is %s, so the window cannot be measured"
                % (status, source))

    left = when - int(now)
    hours = abs(left) / 3600.0
    if left <= 0:
        return ("overdue",
                "still %s, %.1f hour(s) past the close of its window (from %s). "
                "The rows that have not run are not going to." % (status, hours, source))
    if left <= warn_hours * 3600:
        return ("expiring-soon",
                "%.1f hour(s) of window left (from %s) with %s done. Submit the "
                "tail as a second batch while there is still time."
                % (hours, source, rows))
    return ("in-flight",
            "%.1f hour(s) of window left (from %s); %s done" % (hours, source, rows))


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI: the key is wrong, revoked, or belongs "
                         "to another project")
    r.raise_for_status()
    return r.json()


def batches(session, page_size, max_pages):
    """Walk GET /v1/batches, which paginates on the id of the last object."""
    params = {"limit": page_size}
    for _ in range(max_pages):
        page = get(session, "/batches", params)
        data = page.get("data") or []
        for batch in data:
            yield batch
        if not page.get("has_more") or not data:
            return
        params = {"limit": page_size, "after": data[-1].get("id")}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warn-hours", type=float, default=4.0,
                    help="call out in-flight batches with less than this many "
                         "hours of window left (default 4, the 20 hour mark)")
    ap.add_argument("--limit", type=int, default=100,
                    help="page size for GET /v1/batches (default 100)")
    ap.add_argument("--pages", type=int, default=20,
                    help="stop after this many pages (default 20)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print settled batches")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY (a project key set to Read Only)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    now = int(time.time())
    checked = 0
    expired = 0
    closing = 0
    for batch in batches(session, args.limit, args.pages):
        state, detail = verdict(batch, now, args.warn_hours)
        batch_id = str(batch.get("id") or "?")
        line = "%-15s %s  %s" % (state, batch_id, detail)
        checked += 1

        if state == "expired":
            expired += 1
            log.warning(line)
            error_file = batch.get("error_file_id")
            log.warning("  repair: rebuild a .jsonl of the custom_ids whose "
                        "error.code is batch_expired%s and re-submit them, then "
                        "split future jobs so one batch stays well under 50,000 "
                        "requests",
                        (" from GET /v1/files/%s/content" % error_file)
                        if error_file else "")
        elif state in ("overdue", "expiring-soon"):
            closing += 1
            log.warning(line)
            log.warning("  repair: store expires_at in your own job table and "
                        "alert at the 20 hour mark; a poller that waits for "
                        "status == completed waits forever on an expired batch")
        elif state == "unreadable":
            log.warning(line)
        elif args.show_all or state == "in-flight":
            log.info(line)

    log.info("%d batch(es) checked, %d expired, %d close to expiring",
             checked, expired, closing)
    return 1 if (expired or closing) else 0


if __name__ == "__main__":
    sys.exit(main())
