"""Report OpenAI batches that read completed while rows inside them failed.

Read only. GET requests and nothing else: give this a project key set to Read
Only. The repair is printed, never performed, because re-submitting the failed
rows means spending money on inference and that is your decision to make.
"""
import argparse
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_batch_partial_failure_audit")

API = "https://api.openai.com/v1"

# Still moving. None of these is a verdict about the rows, because the counts
# are not final until the batch stops.
IN_FLIGHT = ("validating", "in_progress", "finalizing", "cancelling")

# Terminal, and owned by the sibling notes rather than by this script: a failed
# batch never ran a single row, an expired one ran out of window, a cancelled
# one was stopped on purpose.
OTHER_TERMINAL = ("failed", "expired", "cancelled")

FINDINGS = ("partial", "unaccounted")


def counts_of(batch):
    """Read request_counts into three ints, or None when it cannot be read.

    Pure. Missing members are read as zero because the API omits nothing here,
    but a request_counts that is not an object at all returns None rather than
    three zeros: three zeros would classify as an empty batch, which is a
    completely different and much calmer finding than an unreadable one.
    """
    counts = batch.get("request_counts")
    if not isinstance(counts, dict):
        return None
    try:
        total = int(counts.get("total") or 0)
        done = int(counts.get("completed") or 0)
        failed = int(counts.get("failed") or 0)
    except (TypeError, ValueError):
        return None
    return (total, done, failed)


def verdict(batch):
    """Classify one object from GET /v1/batches. Pure.

    Returns (state, detail). The two findings are kept apart on purpose:
    "partial" is rows that ran and failed, which are in the error file, and
    "unaccounted" is rows that are in neither column, which are not.
    """
    status = str(batch.get("status") or "").strip().lower()

    if status in IN_FLIGHT:
        return ("running",
                "status is %s, so the counts are not final and there is nothing "
                "to reconcile yet" % status)
    if status in OTHER_TERMINAL:
        return ("other-terminal",
                "status is %s. The batch did not finish running, which is a "
                "different problem from finishing with failures inside it."
                % status)
    if status != "completed":
        return ("unreadable",
                "status is %r, which is not a lifecycle state this script "
                "recognises. Read the object by hand." % (status or None,))

    numbers = counts_of(batch)
    if numbers is None:
        return ("unreadable",
                "the batch says completed and carries no readable "
                "request_counts, so nothing here can be reconciled. That is not "
                "the same as a clean batch and is not reported as one.")

    total, done, failed = numbers
    if total <= 0:
        return ("empty",
                "completed with a total of 0 request(s). The input file was "
                "empty or never parsed into rows.")
    if failed > 0:
        return ("partial",
                "%d of %d row(s) failed and the batch still reads completed. "
                "The output file is %d line(s) shorter than the input file."
                % (failed, total, total - done))
    if done < total:
        return ("unaccounted",
                "%d of %d row(s) are neither completed nor failed. Rows in "
                "neither column were abandoned rather than attempted, which is "
                "what a closed completion window looks like in the counts."
                % (total - done, total))
    return ("clean", "all %d row(s) completed" % total)


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
    ap.add_argument("--limit", type=int, default=100,
                    help="page size for GET /v1/batches (default 100)")
    ap.add_argument("--pages", type=int, default=20,
                    help="stop after this many pages (default 20)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print batches that reconcile")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY (a project key set to Read Only)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    checked = 0
    bad = 0
    for batch in batches(session, args.limit, args.pages):
        state, detail = verdict(batch)
        batch_id = str(batch.get("id") or "?")
        line = "%-15s %s  %s" % (state, batch_id, detail)

        if state in FINDINGS:
            checked += 1
            bad += 1
            log.warning(line)
            error_file = batch.get("error_file_id")
            if error_file:
                log.warning("  repair: read the failures with GET "
                            "/v1/files/%s/content, bucket the lines by "
                            "error.code, and re-submit the failed custom_ids as "
                            "a new batch", error_file)
            else:
                log.warning("  repair: no error_file_id on this batch, so the "
                            "missing rows were never attempted. Re-submit them "
                            "and reconcile output lines against input lines.")
            log.warning("  repair: treat request_counts.failed > 0 as a job "
                        "failure in your orchestrator instead of trusting "
                        "status == completed")
        elif state == "clean":
            checked += 1
            if args.show_all:
                log.info(line)
        elif state in ("unreadable", "empty"):
            checked += 1
            log.warning(line)
        elif args.show_all:
            log.info(line)

    log.info("%d completed batch(es) checked, %d with rows missing", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
