"""Find OpenAI vector stores that will delete themselves, and ones that have.

Read only. One paged GET against /v1/vector_stores. No request body is
constructed and no file_search query is ever run.

expires_after is {"anchor": "last_active_at", "days": N} and the anchor is not
a choice: last_active_at is the only supported value, so every expiration
policy on the platform is an idle timer. When it fires the store's status
becomes "expired" and the vector_store.file objects it contained are deleted,
which no read call can undo.

Decisions are made on the expires_at the API returns. The obvious alternative,
last_active_at + days, is computed too and reported as a drift, because which
operations reset the anchor is not something the object or the reference
states. Printing the difference is honest; resolving it would be a guess.
"""
import argparse
import logging
import os
import re
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_vector_store_expiry_audit")

API = "https://api.openai.com/v1"
BETA = {"OpenAI-Beta": "assistants=v2"}
DAY = 86400

# The only anchor the API supports. Anything else is a platform change worth
# reading about rather than a misconfiguration worth correcting.
ANCHOR = "last_active_at"

FINDINGS = ("expired", "policy-on-permanent", "expiring-soon")


def id_set(*raw):
    """The store ids the team treats as permanent. Pure. Order irrelevant."""
    out = set()
    for chunk in raw:
        if not chunk:
            continue
        items = chunk if isinstance(chunk, (list, tuple)) else [chunk]
        for item in items:
            for token in re.split(r"[,\s]+", str(item or "").strip()):
                if token.strip():
                    out.add(token.strip())
    return out


def policy(store):
    """(anchor, days) from expires_after, or None. Pure.

    A policy with a missing or unparseable day count reads as no policy rather
    than as a zero-day one, because a zero would grade every such store as
    already expiring and the object never actually says that.
    """
    raw = (store or {}).get("expires_after")
    if not isinstance(raw, dict):
        return None
    try:
        days = int(raw.get("days"))
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    anchor = str(raw.get("anchor") or "").strip().lower() or ANCHOR
    return (anchor, days)


def expiry_at(store):
    """expires_at as an integer, or None. Pure."""
    try:
        value = int((store or {}).get("expires_at") or 0)
    except (TypeError, ValueError):
        return None
    return value or None


def idle_seconds(store, now):
    """Seconds since last_active_at, or None when the field is absent. Pure."""
    try:
        last = int((store or {}).get("last_active_at") or 0)
    except (TypeError, ValueError):
        return None
    return (now - last) if last > 0 else None


def drift_seconds(store):
    """reported expires_at minus last_active_at + days. Pure. None if unknown.

    Never used to override the reported value. It exists so that a large gap is
    visible, because the definition of activity that would explain it is not
    published anywhere a script can read.
    """
    pol = policy(store)
    reported = expiry_at(store)
    if not pol or reported is None:
        return None
    try:
        last = int((store or {}).get("last_active_at") or 0)
    except (TypeError, ValueError):
        return None
    if last <= 0:
        return None
    return reported - (last + pol[1] * DAY)


def anchor_note(store):
    """A line about an unexpected anchor, or None. Pure."""
    pol = policy(store)
    if pol and pol[0] != ANCHOR:
        return ("expires_after.anchor is %r and the only documented value is "
                "%r. Read the reference before treating this as a "
                "misconfiguration." % (pol[0], ANCHOR))
    return None


def expiry_state(store, now, permanent=(), notice_days=7):
    """Classify one store's clock. Pure. Returns (state, detail).

    status is read before anything else. An expired store has already lost the
    files it held, so it does not share a repair with a store that is merely
    close to the same fate.
    """
    store = store or {}
    sid = str(store.get("id") or "")
    pol = policy(store)
    reported = expiry_at(store)
    idle = idle_seconds(store, now)

    if str(store.get("status") or "").strip().lower() == "expired":
        ago = ""
        if reported:
            ago = " %.0f day(s) ago" % max((now - reported) / DAY, 0)
        return ("expired",
                "expired%s. The contained file objects were deleted and are "
                "not recoverable." % ago)

    if not pol:
        try:
            size = int(store.get("usage_bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        return ("permanent",
                "no policy, %.1f MiB retained and billed" % (size / 1048576.0))

    left = ((reported - now) / DAY) if reported else None
    left_text = ("%.1f day(s) left" % left) if left is not None else \
        "no expires_at reported"

    if sid in set(permanent or ()):
        return ("policy-on-permanent",
                "%d day idle timer on a store you listed as permanent, %s"
                % (pol[1], left_text))
    if left is not None and left <= notice_days:
        idle_text = (", idle for %.1f day(s)" % (idle / DAY)) if idle else ""
        return ("expiring-soon", "%s%s" % (left_text, idle_text))
    return ("scheduled", "%d day idle timer, %s" % (pol[1], left_text))


def repair_lines(state, store=None):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state == "expired":
        return ["re-ingest into a new store. Clearing the policy on this one "
                "changes nothing, because the files it held are already gone.",
                "set the policy you actually want on the new store at creation, "
                "and put whatever produced the corpus into source control so "
                "the next re-ingest is a command rather than an afternoon."]
    if state == "policy-on-permanent":
        return ["clear it by updating expires_after to null on the store. The "
                "listing is a read; the clear is a write and is yours to run.",
                "the anchor is last_active_at and cannot be changed, so a "
                "permanent store cannot be expressed as a long policy. It has "
                "to be no policy at all."]
    if state == "expiring-soon":
        return ["decide which this store is before the date. Temporary is "
                "fine and needs no change; permanent means clearing the policy "
                "now rather than after the files are deleted.",
                "run this check on a schedule shorter than the smallest days "
                "value it reports, or it will tell you about the deletion "
                "afterwards."]
    if state == "permanent":
        return ["nothing expires here, which also means nothing is reclaimed. "
                "Retained bytes are billed by the hour whether or not anything "
                "queries them."]
    return []


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=90)
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
    ap.add_argument("--notice-days", type=float, default=7.0,
                    help="how far ahead an expiry counts as soon")
    ap.add_argument("--permanent", action="append", default=[],
                    help="a store id your team treats as permanent (repeatable)")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key for the project that "
                  "owns the vector stores")
        return 2

    permanent = id_set(os.environ.get("PERMANENT_VECTOR_STORE_IDS"),
                       args.permanent)

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + key, **BETA})

    stores = list(paged(s, "/vector_stores", limit=100))
    with_policy = [st for st in stores if policy(st)]
    log.info("%d store(s) visible to this key, %d with an expiration policy",
             len(stores), len(with_policy))

    now = int(time.time())
    findings = 0
    for store in stores:
        sid = (store or {}).get("id") or "?"
        name = (store or {}).get("name") or "(unnamed)"
        state, detail = expiry_state(store, now, permanent, args.notice_days)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-20s %s %s: %s", state, sid, name, detail)
        for line in repair_lines(state, store):
            emit("  repair: %s", line)
        note = anchor_note(store)
        if note:
            emit("  anchor: %s", note)
        drift = drift_seconds(store)
        if drift is not None and abs(drift) > 3600:
            emit("  drift: reported expires_at is %.1fh %s last_active_at plus "
                 "the policy window", abs(drift) / 3600.0,
                 "ahead of" if drift > 0 else "behind")
        if state in FINDINGS:
            findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
