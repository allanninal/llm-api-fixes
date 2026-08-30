"""Check that the vector store ids your application configures index anything.

Read only. One GET per configured id against /v1/vector_stores/{id}, plus a
paged GET of /v1/vector_stores for the wider picture. No request body is
constructed and no file_search query is ever run, because a retrieval query is
a generation and this script exists to say whether the index is empty, not to
find out what it would answer.

The configured ids are the input, and that is the whole design. An empty vector
store is an ordinary object; it only becomes a fault when something still names
it in vector_store_ids. So this reads your configuration first and the platform
second, which is the reverse of every other note in this batch.
"""
import argparse
import logging
import os
import re
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_empty_vector_store_audit")

API = "https://api.openai.com/v1"

# The official client still sends this on every vector store call, so this
# script does too. It is a GET either way.
BETA = {"OpenAI-Beta": "assistants=v2"}

FINDINGS = ("referenced-empty", "referenced-nothing-indexed",
            "referenced-zero-bytes", "referenced-missing")

CAUSES = {
    "expired": "the store passed its expiration policy and deleted its own "
               "files. That is the expiry note, and it will happen again on "
               "the same schedule.",
    "attach-failed": "files were attached and none of them indexed. That is "
                     "the attach failure note: bucket the children by "
                     "last_error.code and repair per bucket, not per store.",
    "still-ingesting": "files are still processing. You are early rather than "
                       "broken; re-read once file_counts.in_progress is zero.",
    "never-ingested": "the ingest never ran against this store. Nothing was "
                      "ever attached to it.",
}


def configured_ids(*raw):
    """The store ids the application claims to use. Pure.

    Split on commas or whitespace, blanks dropped, order preserved, duplicates
    removed. A trailing comma in an environment variable is the common way an
    empty string becomes an id that 404s and gets reported as a missing store.
    """
    out, seen = [], set()
    for chunk in raw:
        if not chunk:
            continue
        items = chunk if isinstance(chunk, (list, tuple)) else [chunk]
        for item in items:
            for token in re.split(r"[,\s]+", str(item or "").strip()):
                token = token.strip()
                if token and token not in seen:
                    seen.add(token)
                    out.append(token)
    return out


def counts(store):
    """The five file_counts integers, coerced. Pure."""
    raw = (store or {}).get("file_counts") or {}
    out = {}
    for key in ("in_progress", "completed", "failed", "cancelled", "total"):
        try:
            out[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def usage_bytes(store):
    """usage_bytes as an integer. Pure. Missing or unparseable reads as 0."""
    try:
        return int((store or {}).get("usage_bytes") or 0)
    except (TypeError, ValueError):
        return 0


def emptiness(store):
    """How empty one store is. Pure. One of four words, tested in order.

    The order carries the meaning. total == 0 says nothing was ever attached;
    completed == 0 with files present says the attach failed. Running the tests
    the other way round reports every failed ingest as an empty store and sends
    the repair to the wrong place.
    """
    c = counts(store)
    if c["total"] <= 0:
        return "no-files"
    if c["completed"] <= 0:
        return "nothing-completed"
    if usage_bytes(store) <= 0:
        return "zero-bytes"
    return "indexed"


def cause(store):
    """Why the store is empty, as far as the object can say. Pure.

    Returns a key into CAUSES. status is read first because expiry is the one
    cause that recurs: an expired store deletes its contained files, so the
    counts afterwards look exactly like an ingest that never ran.
    """
    if str((store or {}).get("status") or "").strip().lower() == "expired":
        return "expired"
    c = counts(store)
    if c["failed"] > 0:
        return "attach-failed"
    if c["in_progress"] > 0:
        return "still-ingesting"
    return "never-ingested"


def classify(store, referenced):
    """Grade one store. Pure. Returns (state, detail).

    A store is only graded against whether something references it. Emptiness
    on its own bills nothing and grounds nothing, and reporting it at finding
    severity is how a report teaches people to skim it.
    """
    if store is None:
        if referenced:
            return ("referenced-missing",
                    "no such store for this key. Vector stores are project "
                    "scoped, so the usual cause is a key from the wrong "
                    "project rather than a deleted store.")
        return ("not-found", "no such store")

    c = counts(store)
    kind = emptiness(store)
    size = usage_bytes(store)

    if not referenced:
        if kind == "indexed":
            return ("unreferenced",
                    "%d file(s) completed, and nothing you passed names it"
                    % c["completed"])
        return ("abandoned-empty",
                "empty and unreferenced, which is litter rather than an outage")

    if kind == "no-files":
        return ("referenced-empty",
                "0 file(s) attached, 0 bytes")
    if kind == "nothing-completed":
        return ("referenced-nothing-indexed",
                "%d attached, 0 completed, %d failed, %d in progress"
                % (c["total"], c["failed"], c["in_progress"]))
    if kind == "zero-bytes":
        return ("referenced-zero-bytes",
                "%d file(s) report completed and usage_bytes is 0, which the "
                "three emptiness tests disagree about. Read it before acting."
                % c["completed"])
    return ("grounded",
            "%d file(s) completed, %.1f MiB" % (c["completed"], size / 1048576.0))


def repair_lines(state, why=None):
    """The repair for one verdict. Pure. Printed, never performed."""
    assertion = ("assert file_counts.completed > 0 for every id in "
                 "vector_store_ids at startup and refuse to boot. A retrieval "
                 "feature that cannot retrieve should fail at deploy, not in "
                 "an answer.")
    if state == "referenced-empty":
        lines = []
        if why == "expired":
            lines.append(CAUSES["expired"])
        lines.append("run the ingest, then re-read the store before shipping "
                     "the id.")
        lines.append(assertion)
        return lines
    if state == "referenced-nothing-indexed":
        return [CAUSES.get(why or "attach-failed", CAUSES["attach-failed"]),
                assertion]
    if state == "referenced-zero-bytes":
        return ["do not delete this one on the strength of a byte count. Read "
                "the store and one of its files before deciding what it is.",
                assertion]
    if state == "referenced-missing":
        return ["check the project first. A project key cannot see a store "
                "that lives in another project, and that 404 is identical to "
                "the one a deleted store returns.",
                "if the store really is gone, re-ingest and update the "
                "configured id in the same change.",
                assertion]
    if state == "abandoned-empty":
        return ["nothing references it and it holds no bytes, so it is not "
                "costing you anything. Delete it when convenient with "
                "DELETE /v1/vector_stores/{vector_store_id}."]
    return []


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/vector_stores needs a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def get_optional(session, path):
    """One store, or None when it does not resolve for this key."""
    r = session.get(API + path, timeout=90)
    if r.status_code == 404:
        return None
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/vector_stores needs a project key"
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
                    help="a store id your application configures (repeatable)")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key for the project that "
                  "owns the vector stores")
        return 2

    wanted = configured_ids(os.environ.get("VECTOR_STORE_IDS"), args.store_id)
    if not wanted:
        log.error("pass the store ids your application configures, as "
                  "VECTOR_STORE_IDS or repeated --store-id. Without them this "
                  "script has nothing to grade: an empty store is only a "
                  "finding when something still names it.")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + key, **BETA})

    stores = list(paged(s, "/vector_stores", limit=100))
    by_id = {(st or {}).get("id"): st for st in stores}
    log.info("%d configured id(s), %d store(s) visible to this key",
             len(wanted), len(stores))

    findings = 0
    for sid in wanted:
        store = by_id.get(sid)
        if store is None:
            store = get_optional(s, "/vector_stores/%s" % sid)
        state, detail = classify(store, referenced=True)
        why = cause(store) if store is not None else None
        name = (store or {}).get("name") or ""
        emit = log.warning if state in FINDINGS else log.info
        emit("%-26s %s %s: %s", state, sid, name, detail)
        if state in FINDINGS and store is not None:
            emit("  cause: %s", CAUSES[why])
        for line in repair_lines(state, why):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    litter = [st for st in stores
              if (st or {}).get("id") not in set(wanted)
              and emptiness(st) != "indexed"]
    if litter:
        log.info("%-26s %d empty store(s) nothing references, which is litter",
                 "abandoned-empty", len(litter))
        for line in repair_lines("abandoned-empty"):
            log.info("  note: %s", line)

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
