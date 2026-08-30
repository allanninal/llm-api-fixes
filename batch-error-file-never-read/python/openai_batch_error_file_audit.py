"""Report OpenAI batch error files that exist and were never fetched.

Read only. Two GET requests and nothing else: give this a project key set to
Read Only. The repair is printed, never performed.

The API cannot tell you whether you read a file: there is no last_accessed_at
on a file object and no access log to query. So the second half of this check
comes from you, as a list of error file ids your ingest has consumed. Absence
of that list is itself an answer.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_batch_error_file_audit")

API = "https://api.openai.com/v1"
DAY = 86400

# Batch input, output and error files are retained for 30 days from creation.
# After that the content is unrecoverable by any read call.
RETENTION_DAYS = 30

IN_FLIGHT = ("validating", "in_progress", "finalizing", "cancelling")

FINDINGS = ("unread", "expiring", "aged-out")


def days_left(created_at, now, retention_days=RETENTION_DAYS):
    """Whole days of retention left on a file, or None if unreadable. Pure.

    Floors the elapsed time, so a file created 29.9 days ago has 1 day left
    rather than 0.1: this number is printed to a human who will act on it
    tomorrow, and rounding it the other way promises time that is not there.
    """
    try:
        created = int(created_at)
    except (TypeError, ValueError):
        return None
    if created <= 0:
        return None
    return retention_days - int((int(now) - created) // DAY)


def verdict(batch, file_meta, fetched, now, retention_days=RETENTION_DAYS,
            urgent_days=3):
    """Classify one batch against its error file and your ingest record. Pure.

    file_meta is the object from GET /v1/files/{id}, or None when that call
    found nothing. fetched is the set of error file ids your pipeline has
    consumed. now is unix seconds, passed in so the retention boundary can be
    tested at a fixed instant. Returns (state, detail).
    """
    status = str(batch.get("status") or "").strip().lower()
    file_id = str(batch.get("error_file_id") or "").strip()

    if status in IN_FLIGHT:
        return ("running",
                "status is %s; an error file is not final until the batch stops"
                % status)
    if not file_id:
        return ("no-error-file",
                "no error_file_id on this batch, so nothing failed hard enough "
                "to be written to one")
    if file_id in set(fetched or ()):
        return ("fetched",
                "error file %s is in the ingest record, so the failures were "
                "read" % file_id)

    created = None
    if isinstance(file_meta, dict):
        created = file_meta.get("created_at")
    if not created:
        created = batch.get("created_at")
    left = days_left(created, now, retention_days)

    if not isinstance(file_meta, dict):
        if left is not None and left <= 0:
            return ("aged-out",
                    "error file %s is past the %d day retention window and "
                    "GET /v1/files no longer returns it. Which rows failed, and "
                    "why, cannot be recovered by any read call now."
                    % (file_id, retention_days))
        return ("unresolvable",
                "the batch names error file %s but GET /v1/files/%s returned "
                "nothing, and the file is still inside the retention window. "
                "Check that id by hand." % (file_id, file_id))

    try:
        size = int(file_meta.get("bytes") or 0)
    except (TypeError, ValueError):
        size = 0

    if size <= 0:
        return ("empty",
                "error file %s exists and holds 0 byte(s). The id was allocated "
                "and never written to, so there is nothing in it to read."
                % file_id)
    if left is not None and left <= 0:
        return ("aged-out",
                "error file %s holds %d byte(s) that are past the %d day "
                "retention window. The metadata is still listed; the content is "
                "not retrievable." % (file_id, size, retention_days))
    if left is not None and left <= urgent_days:
        return ("expiring",
                "error file %s holds %d byte(s), is not in the ingest record, "
                "and expires in %d day(s). Download it before the window closes."
                % (file_id, size, left))
    return ("unread",
            "error file %s holds %d byte(s) and is not in the ingest record. "
            "Every line in it is a row missing from the downstream table."
            % (file_id, size))


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI: the key is wrong, revoked, or belongs "
                         "to another project")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def batches(session, page_size, max_pages):
    """Walk GET /v1/batches, which paginates on the id of the last object."""
    params = {"limit": page_size}
    for _ in range(max_pages):
        page = get(session, "/batches", params)
        data = (page or {}).get("data") or []
        for batch in data:
            yield batch
        if not (page or {}).get("has_more") or not data:
            return
        params = {"limit": page_size, "after": data[-1].get("id")}


def read_fetched(args):
    """The error file ids your pipeline says it consumed. Local reads only."""
    ids = set(args.fetched)
    if args.fetched_file:
        with open(args.fetched_file, "r", encoding="utf-8") as fh:
            ids.update(line.strip() for line in fh if line.strip())
    return ids


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetched", action="append", default=[],
                    help="an error file id your pipeline has consumed; repeatable")
    ap.add_argument("--fetched-file",
                    help="a file of error file ids your pipeline has consumed, "
                         "one per line")
    ap.add_argument("--limit", type=int, default=100,
                    help="page size for GET /v1/batches (default 100)")
    ap.add_argument("--pages", type=int, default=20,
                    help="stop after this many pages (default 20)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print batches with nothing to fetch")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY (a project key set to Read Only)")
        return 2

    fetched = read_fetched(args)
    if not fetched:
        log.info("no ingest record passed, so every error file will be reported "
                 "as unread. Pass --fetched or --fetched-file once you have one.")

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    now = int(time.time())
    with_file = 0
    bad = 0
    for batch in batches(session, args.limit, args.pages):
        file_id = str(batch.get("error_file_id") or "").strip()
        file_meta = get(session, "/files/" + file_id) if file_id else None

        state, detail = verdict(batch, file_meta, fetched, now)
        batch_id = str(batch.get("id") or "?")
        line = "%-15s %s  %s" % (state, batch_id, detail)

        if file_id:
            with_file += 1
        if state in FINDINGS:
            bad += 1
            log.warning(line)
            if state == "aged-out":
                log.warning("  repair: the content is gone. Re-run the batch "
                            "from the original input file and diff the output "
                            "custom_ids against it to find the missing rows.")
            else:
                log.warning("  repair: GET /v1/files/%s/content, group the lines "
                            "by error.code, retry the transient ones "
                            "(rate_limit_exceeded, server_error) as a new batch, "
                            "and fix the rest", file_id)
            log.warning("  repair: assert error_file_id is null in the "
                        "batch-completion handler rather than checking it by hand "
                        "once a year")
        elif state == "unresolvable":
            log.warning(line)
        elif args.show_all or state == "empty":
            log.info(line)

    log.info("%d batch(es) with an error file, %d never fetched", with_file, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
