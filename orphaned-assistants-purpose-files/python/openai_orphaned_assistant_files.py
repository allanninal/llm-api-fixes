"""Subtract the ids surviving vector stores hold from one dead purpose class.

Read only. Four kinds of GET: two file listings, the vector store listing, and
one file listing per store. Nothing is created and nothing is deleted.

The Assistants API reached its shutdown date on 2026-08-26. Its objects went;
the files they referenced did not, because deleting an API does not delete
storage. `assistants` and `assistants_output` are still valid values of
`purpose` on the File object, so those files still enumerate and still count
against the project's storage ceiling.

One honesty note carried into the output. OpenAI's migration guide covers
assistants, threads and runs and says nothing whatsoever about files, vector
stores or purposes, so "a file in no surviving store has no owner" is an
inference from what is readable rather than a documented fact. What is
documented is that the two purposes remain valid, that vector stores are a
live resource, and that deleting a file removes it from every vector store.

The subtraction is only as good as the set being subtracted, so a store whose
file listing could not be read downgrades every verdict in the run.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_orphaned_assistant_files")

FILES_URL = "https://api.openai.com/v1/files"
STORES_URL = "https://api.openai.com/v1/vector_stores"

PURPOSES = ("assistants", "assistants_output")
FINDINGS = ("orphan", "orphan-output", "subtraction-incomplete")

# The files listing accepts up to 10,000 per page; both vector store listings
# cap at 100. Copying the first number onto the second endpoint is how a
# referenced set silently loses everything after row 100.
FILE_PAGE = 10000
STORE_PAGE = 100


def file_row(body):
    """One file object, reduced. Pure."""
    body = body if isinstance(body, dict) else {}
    try:
        size = int(body.get("bytes"))
    except (TypeError, ValueError):
        size = 0
    try:
        created = int(body.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0
    return {"id": str(body.get("id") or ""),
            "filename": str(body.get("filename") or ""),
            "size": max(0, size),
            "purpose": str(body.get("purpose") or ""),
            "created_at": max(0, created)}


def referenced_ids(store_files):
    """The set to subtract. Pure.

    A vector_store.file object's own `id` is the underlying Files API id, so
    membership is one field rather than a join.
    """
    out = set()
    for item in store_files or []:
        if isinstance(item, dict):
            fid = str(item.get("id") or "")
            if fid:
                out.add(fid)
    return out


def human(size):
    """Binary units, one decimal. Pure."""
    try:
        n = float(size)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return "%d B" % int(n) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TiB" % n


def age_days(created_at, now):
    """Age in days. Pure. The clock is an argument. None when undatable."""
    try:
        created, at = int(created_at), int(now)
    except (TypeError, ValueError):
        return None
    return (at - created) / 86400.0 if created > 0 else None


def class_state(rows, complete):
    """Grade the purpose class as a whole. Pure. Returns (state, detail)."""
    if not complete:
        return ("subtraction-unsafe",
                "the referenced set is incomplete, so no file in this class "
                "can be called an orphan")
    if not rows:
        return ("class-empty",
                "no file carries purpose assistants or assistants_output, so "
                "nothing was left behind here")
    return ("class-populated",
            "%d file(s) carry a purpose whose owning API no longer exists"
            % len(rows))


def classify_file(row, referenced, complete, now):
    """Grade one file. Pure. Completeness is tested before anything else.

    Deliberately first: when the referenced set is partial, a file that is in
    a store the script could not read is indistinguishable from an orphan, and
    the output of this script is a list of deletion commands.
    """
    row = row if isinstance(row, dict) else {}
    fid = str(row.get("id") or "")
    if not complete:
        return ("subtraction-incomplete",
                "%s: at least one vector store could not be listed, so this "
                "file cannot be called an orphan" % fid)
    if fid in (referenced or set()):
        return ("still-referenced",
                "%s: held by a live vector store, so file search under the "
                "Responses API still reads it" % fid)
    age = age_days(row.get("created_at"), now)
    when = ("created %.0f day(s) ago" % age) if age is not None else "undated"
    if row.get("purpose") == "assistants_output":
        return ("orphan-output",
                "%s: code interpreter output from a run that no longer exists, "
                "%s, %s" % (fid, human(row.get("size")), when))
    return ("orphan",
            "%s: no surviving vector store holds this id, %s, %s"
            % (fid, human(row.get("size")), when))


def summarise(graded):
    """Fold graded rows into per-state counts and bytes. Pure."""
    acc = {}
    for state, row in graded or []:
        cur = acc.setdefault(state, {"count": 0, "bytes": 0})
        cur["count"] += 1
        cur["bytes"] += int((row or {}).get("size") or 0)
    return acc


def repair_lines(state, orphan_count=0, orphan_bytes=0, unreadable=()):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state in ("orphan", "orphan-output"):
        return ["%d confirmed orphan(s), %s. Archive anything you still want, "
                "then DELETE /v1/files/{file_id} one at a time. The delete "
                "also removes the file from every vector store holding it."
                % (orphan_count, human(orphan_bytes)),
                "re-upload future file search sources with purpose user_data "
                "and an expires_after policy, so the next class ages out on "
                "its own."]
    if state in ("subtraction-incomplete", "subtraction-unsafe"):
        return ["%d vector store(s) could not be listed: %s. Re-run with a key "
                "that can read them. A set difference against an incomplete "
                "set names files that are perfectly well referenced."
                % (len(unreadable), ", ".join(sorted(unreadable)) or "unknown")]
    if state == "class-empty":
        return []
    return []


def get_page(url, params, key, timeout=30):
    """One GET. Returns (body, ok). A non-200 is a fact, not an exception."""
    try:
        r = requests.get(url, params=params,
                         headers={"Authorization": "Bearer " + key},
                         timeout=timeout)
    except requests.RequestException as exc:
        log.debug("GET %s failed: %s", url, exc)
        return (None, False)
    if r.status_code != 200:
        log.debug("GET %s returned HTTP %s", url, r.status_code)
        return (None, False)
    try:
        return (r.json(), True)
    except ValueError:
        return (None, False)


def walk(url, key, params, page_size, max_pages):
    """Page any of these listings on `after`. Returns (items, ok)."""
    items, cursor, pages = [], None, 0
    while pages < max_pages:
        query = dict(params or {})
        query["limit"] = page_size
        if cursor:
            query["after"] = cursor
        body, ok = get_page(url, query, key)
        if not ok:
            return (items, False)
        data = (body or {}).get("data") or []
        pages += 1
        items.extend(data)
        if not data or (body or {}).get("has_more") is False:
            return (items, True)
        if "has_more" not in (body or {}) and len(data) < page_size:
            return (items, True)
        cursor = data[-1].get("id")
        if not cursor:
            return (items, True)
    return (items, False)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-pages", type=int, default=50,
                    help="page cap applied to every listing")
    ap.add_argument("--show", type=int, default=25,
                    help="how many individual files to print")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key set to Read Only. Every "
                  "call is a GET of /v1/files or /v1/vector_stores")
        return 2

    now = int(time.time())
    rows = []
    for purpose in PURPOSES:
        items, ok = walk(FILES_URL, key, {"purpose": purpose, "order": "asc"},
                         FILE_PAGE, args.max_pages)
        if not ok:
            log.error("the %s listing could not be read in full; nothing can "
                      "be concluded from a partial class", purpose)
            return 2
        rows.extend(file_row(item) for item in items)

    stores, stores_ok = walk(STORES_URL, key, {}, STORE_PAGE, args.max_pages)
    referenced, unreadable = set(), []
    for store in stores:
        sid = str((store or {}).get("id") or "")
        if not sid:
            continue
        items, ok = walk("%s/%s/files" % (STORES_URL, sid), key, {},
                         STORE_PAGE, args.max_pages)
        referenced |= referenced_ids(items)
        if not ok:
            unreadable.append(sid)
    complete = stores_ok and not unreadable

    log.info("%d vector store(s) read, %d referenced file id(s)",
             len(stores), len(referenced))
    counts = {p: sum(1 for r in rows if r["purpose"] == p) for p in PURPOSES}
    log.info("%d file(s) in the class: %d assistants, %d assistants_output, %s",
             len(rows), counts["assistants"], counts["assistants_output"],
             human(sum(r["size"] for r in rows)))
    log.info("  measured: two purpose listings, minus the ids held by every "
             "store read")
    log.info("  inferred: that a file in no surviving store has no owner. The "
             "migration guide documents nothing at all about files or vector "
             "stores")
    if not stores_ok:
        log.warning("  the vector store listing itself was truncated or failed")

    state, detail = class_state(rows, complete)
    (log.warning if state == "subtraction-unsafe" else log.info)(
        "%-20s %s", state, detail)

    graded = [(classify_file(row, referenced, complete, now)[0], row)
              for row in rows]
    shown = 0
    for row in rows:
        verdict, line = classify_file(row, referenced, complete, now)
        if shown < args.show:
            (log.warning if verdict in FINDINGS else log.info)(
                "%-20s %s", verdict, line)
            shown += 1

    totals = summarise(graded)
    orphans = totals.get("orphan", {"count": 0, "bytes": 0})
    outputs = totals.get("orphan-output", {"count": 0, "bytes": 0})
    findings = sum(totals.get(s, {}).get("count", 0) for s in FINDINGS)
    if orphans["count"] or outputs["count"]:
        for line in repair_lines("orphan",
                                 orphans["count"] + outputs["count"],
                                 orphans["bytes"] + outputs["bytes"]):
            log.warning("  repair: %s", line)
    if not complete:
        for line in repair_lines("subtraction-incomplete",
                                 unreadable=unreadable):
            log.warning("  repair: %s", line)

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
