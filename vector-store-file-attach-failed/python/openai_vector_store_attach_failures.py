"""Find files that never indexed in an OpenAI vector store.

Read only. Every request is a GET: /v1/vector_stores for the parents, then
/v1/vector_stores/{id}/files with filter=failed and filter=in_progress for the
children. No request body is ever constructed, and in particular no file_search
query is ever run. A retrieval query is a generation, it is billed, and a script
about a broken index has no business creating traffic against it.

The subject is the child object. A vector_store.file carries last_error.code
with one of exactly three values; the parent carries a failed count that its own
status field does not reflect, because status becomes "completed" when nothing
is still in progress whether or not anything succeeded.

A store with no files at all is not this note, and is reported as such.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_vector_store_attach_failures")

API = "https://api.openai.com/v1"

# The official client still sends this on every vector store call, so this
# script does too rather than betting on where the listing has got to in its
# graduation out of the Assistants beta. It is a GET either way.
BETA = {"OpenAI-Beta": "assistants=v2"}

# The complete set. last_error.code is documented as exactly these three, and a
# fourth arriving is worth reporting rather than bucketing into "other".
ERROR_CODES = ("server_error", "unsupported_file", "invalid_file")

# A failed child whose last_error is null. The field is nullable on every child
# including the failed ones, and a reader that keys on last_error["code"] either
# raises or drops these rows. Dropping them is worse: a failure with no stated
# reason is the one nobody has looked at.
UNREPORTED = "unreported"

REPAIRS = {
    "unsupported_file":
        "unsupported_file is a format the parser cannot read: a scan with no "
        "text layer, or an extension it does not handle. OCR the scans and "
        "export the rest to .md or .txt, then attach again.",
    "invalid_file":
        "invalid_file is usually empty, corrupt or password protected. Fix it "
        "at the source; re-attaching the same bytes fails the same way.",
    "server_error":
        "server_error is transient. Attach those files again and re-check "
        "before treating them as a content problem.",
    UNREPORTED:
        "these failed with no last_error at all. Fetch each one with GET "
        "/v1/vector_stores/{vector_store_id}/files/{file_id} before deciding, "
        "because a failure with no stated reason has not been looked at.",
}

FINDINGS = ("attach-failed", "ingestion-stalled", "counts-disagree")


def counts(store):
    """The five file_counts integers, coerced. Pure.

    A missing key becomes 0 rather than None, so every caller can do arithmetic
    without guarding, and a string that arrives where an integer was promised
    does not propagate into a division.
    """
    raw = (store or {}).get("file_counts") or {}
    out = {}
    for key in ("in_progress", "completed", "failed", "cancelled", "total"):
        try:
            out[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def bucket_errors(files):
    """{last_error.code: [file_id, ...]} over the failed children. Pure.

    Only children whose status is actually "failed" are counted, because the
    filtered listing is a request parameter rather than a guarantee, and a
    caller that forgets the filter would otherwise bucket the whole store.
    """
    out = {}
    for entry in files or []:
        row = entry or {}
        if str(row.get("status") or "").strip().lower() != "failed":
            continue
        err = row.get("last_error") or {}
        code = str(err.get("code") or "").strip().lower() or UNREPORTED
        out.setdefault(code, []).append(str(row.get("id") or "?"))
    for ids in out.values():
        ids.sort()
    return out


def stalled(files, now, max_age=3600):
    """[(file_id, age_seconds)] for children pinned in_progress. Pure.

    Sorted oldest first. A child with no usable created_at is skipped rather
    than treated as infinitely old, which would report every store as stalled
    the first time the field shape changes.
    """
    out = []
    for entry in files or []:
        row = entry or {}
        if str(row.get("status") or "").strip().lower() != "in_progress":
            continue
        try:
            created = int(row.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        if created > 0 and (now - created) > max_age:
            out.append((str(row.get("id") or "?"), int(now - created)))
    out.sort(key=lambda r: (-r[1], r[0]))
    return out


def failure_rate(c):
    """failed / total. Pure. Zero on an empty store rather than an exception."""
    total = (c or {}).get("total") or 0
    if total <= 0:
        return 0.0
    return float((c or {}).get("failed") or 0) / float(total)


def reconcile(c, buckets):
    """(claimed, listed) failure counts. Pure.

    Two numbers, never one. file_counts.failed is a stored aggregate and the
    filtered listing enumerates live children, so they can legitimately differ,
    and averaging them into a single confident number destroys the only signal
    that says a repair was started and abandoned.
    """
    listed = sum(len(v) for v in (buckets or {}).values())
    try:
        claimed = int((c or {}).get("failed") or 0)
    except (TypeError, ValueError):
        claimed = 0
    return (claimed, listed)


def verdict(c, buckets, stalled_rows):
    """Classify one store. Pure. Returns (state, detail).

    The empty case is answered first and handed to the other note by name. A
    store with nothing in it has a zero per cent failure rate, which is true and
    useless, and its repair is re-running an ingest rather than fixing a format.
    """
    c = c or {}
    total = int(c.get("total") or 0)
    claimed, listed = reconcile(c, buckets)
    stalled_rows = list(stalled_rows or [])

    if total <= 0:
        return ("no-files",
                "nothing has ever been attached, so this is the empty vector "
                "store note rather than this one")
    if listed > 0:
        detail = ("%d of %d file(s) failed (%.1f%%)"
                  % (listed, total, failure_rate(c) * 100))
        if claimed != listed:
            detail += (" -- file_counts.failed says %d and the listing returns "
                       "%d, so read the listing" % (claimed, listed))
        return ("attach-failed", detail)
    if claimed > 0:
        return ("counts-disagree",
                "file_counts.failed is %d and the filtered listing returns "
                "none, which is what a half-finished repair looks like: the "
                "failed files were detached and never attached again"
                % claimed)
    if stalled_rows:
        oldest = stalled_rows[0][1] // 3600
        return ("ingestion-stalled",
                "%d file(s) still in_progress, the oldest for over %dh. The "
                "parent stays in_progress while any child is."
                % (len(stalled_rows), max(oldest, 1)))
    if int(c.get("in_progress") or 0) > 0:
        return ("still-ingesting",
                "%d file(s) in_progress and none of them old enough to call "
                "pinned. Re-run after the ingest settles."
                % int(c.get("in_progress") or 0))
    return ("complete",
            "%d file(s), all completed, and the summary agrees with the listing"
            % total)


def repair_lines(state, buckets=None, stalled_rows=()):
    """The repair for one verdict. Pure. Printed, never performed."""
    buckets = buckets or {}
    if state == "attach-failed":
        lines = [REPAIRS[code] for code in
                 sorted(buckets, key=lambda k: (-len(buckets[k]), k))
                 if code in REPAIRS]
        unknown = sorted(set(buckets) - set(REPAIRS))
        if unknown:
            lines.append("last_error.code came back as %s, which is not one of "
                         "the three documented values. Read the message field "
                         "before acting on it." % ", ".join(unknown))
        lines.append("gate the ingest job on file_counts.failed == 0, not on "
                     "status == \"completed\", which only means nothing is "
                     "pending.")
        return lines
    if state == "counts-disagree":
        return [
            "list the store's files without a filter and compare the ids "
            "against your ingest manifest. The failures are gone from the "
            "store and are still missing from retrieval.",
            "re-attach the manifest entries that no longer appear, then assert "
            "file_counts.failed == 0 and file_counts.completed == "
            "file_counts.total before declaring the store ready.",
        ]
    if state == "ingestion-stalled":
        oldest = list(stalled_rows or [])[:5]
        lines = ["detach and attach those files again rather than waiting. A "
                 "child pinned for hours is not going to finish on its own."]
        if oldest:
            lines.append("oldest pinned: " + ", ".join(
                "%s (%dh)" % (fid, age // 3600) for fid, age in oldest))
        lines.append("stagger large ingests, and poll file_counts.in_progress "
                     "down to zero with a timeout rather than assuming that "
                     "attach means indexed.")
        return lines
    if state == "no-files":
        return ["an empty store fails differently and is repaired differently. "
                "Re-run the ingest, or stop naming the store in "
                "vector_store_ids."]
    return []


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/vector_stores needs a project "
                         "key for the project that owns the stores"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def paged(session, path, max_pages=200, **params):
    """Walk an after/last_id cursor listing."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, path, **params)
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params["after"] = page.get("last_id") or (data[-1] or {}).get("id")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store-id", action="append", default=[],
                    help="restrict to these store ids (repeatable)")
    ap.add_argument("--stalled-hours", type=float, default=1.0,
                    help="age at which an in_progress file is called pinned")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key for the project that "
                  "owns the vector stores")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + key, **BETA})

    stores = list(paged(s, "/vector_stores", limit=100))
    wanted = set(args.store_id or [])
    if wanted:
        stores = [st for st in stores if (st or {}).get("id") in wanted]
    log.info("%d store(s) visible to this key", len(stores))

    now = int(time.time())
    max_age = int(args.stalled_hours * 3600)
    findings = 0

    for store in stores:
        sid = (store or {}).get("id") or "?"
        name = (store or {}).get("name") or "(unnamed)"
        c = counts(store)

        failed = []
        pending = []
        if c["total"] > 0:
            failed = list(paged(s, "/vector_stores/%s/files" % sid,
                                limit=100, filter="failed"))
            if c["in_progress"] > 0:
                pending = list(paged(s, "/vector_stores/%s/files" % sid,
                                     limit=100, filter="in_progress"))

        buckets = bucket_errors(failed)
        stalled_rows = stalled(pending, now, max_age)
        state, detail = verdict(c, buckets, stalled_rows)

        emit = log.warning if state in FINDINGS else log.info
        emit("%-20s %s %s: %s", state, sid, name, detail)
        if state == "attach-failed":
            for code in sorted(buckets, key=lambda k: (-len(buckets[k]), k)):
                ids = buckets[code]
                shown = ", ".join(ids[:3]) + (" ..." if len(ids) > 3 else "")
                emit("  %-18s %d file(s)  %s", code, len(ids), shown)
        for line in repair_lines(state, buckets, stalled_rows):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
