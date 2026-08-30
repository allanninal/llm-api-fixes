"""Inventory a capability that is being withdrawn, with no successor to move to.

Read only. Every request is a GET: the model objects for the five ids the
deprecation table names, the video listing, and the organization cost report.
Nothing here renders a video, and no request in this script creates anything.

Two things make this different from a model retirement. The deprecation table
lists no replacement for any Sora id, so the repair is a removal rather than a
substitution -- REPLACEMENTS below is empty on purpose and there is a test that
keeps it empty. And every rendered asset carries its own expires_at, so each
file has two deadlines and needs the earlier one.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sora_shutdown_inventory")

API = "https://api.openai.com/v1"

# Announced 24 March 2026. The Videos API and every Sora 2 model close on this
# date. Published, and also readable: shutdown_date on the model object is the
# authority, and this constant is the fallback when the object carries no date.
SHUTDOWN = "2026-09-24"

# The five ids the deprecation table names.
SORA_IDS = ("sora-2", "sora-2-pro", "sora-2-2025-10-06", "sora-2-2025-12-08",
            "sora-2-pro-2025-10-06")

# Empty on purpose, and kept empty by a test. Every Sora row in the deprecation
# table has an empty replacement column, because what is being withdrawn is a
# capability and not a model. The failure mode for a script about a capability
# removal is that a later reader fills this in with the closest-looking model
# id, at which point the script lies confidently.
REPLACEMENTS = {}

FINDINGS = ("shutdown-dated", "past-shutdown", "already-gone",
            "already-expired", "expires-first", "outlives-the-endpoint",
            "no-asset-expiry", "video-spend-accruing")

REPAIRS = {
    "shutdown-dated":
        "remove the /v1/videos code path and the sora-2 constants. This is a "
        "capability leaving the API, not a model changing name, so the "
        "decision is a third-party provider or dropping the feature.",
    "past-shutdown":
        "the date has passed. Anything still calling this path is returning "
        "404 to somebody right now.",
    "already-gone":
        "this id no longer resolves, so the removal is already overdue for "
        "whatever still names it.",
    "already-expired":
        "these bytes are gone and only the metadata row is left. If the render "
        "mattered, it has to be regenerated before the endpoint closes, which "
        "is the last chance there will be.",
    "expires-first":
        "download these before their own expiry, which lands sooner than the "
        "endpoint shutdown. This is the front of the queue.",
    "outlives-the-endpoint":
        "download these before the shutdown. Their own expiry is later, which "
        "is irrelevant once there is no endpoint left to serve them.",
    "no-asset-expiry":
        "no expiry of their own does not mean no deadline. They inherit the "
        "endpoint's, so they need downloading like everything else.",
    "video-spend-accruing":
        "this is a live feature with money moving through it, not a branch "
        "somebody forgot. Whoever owns the customer-facing promise of video "
        "generation needs the date before engineering picks a plan.",
}


def days_left(today, when=SHUTDOWN):
    """Whole days from today to a date. Pure. Negative once it has passed."""
    return (dt.date.fromisoformat(str(when))
            - dt.date.fromisoformat(str(today))).days


def iso_day(stamp):
    """A unix second stamp as a UTC day, or None. Pure.

    Kept separate from asset_deadline() so the two-clock comparison can be
    tested in dates rather than in timestamps, which is the part of it that is
    easy to get wrong.
    """
    if stamp in (None, "", 0):
        return None
    try:
        return dt.datetime.fromtimestamp(int(stamp),
                                         dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def replacement_for(model_id):
    """The documented successor for one id. Pure. Returns None, every time.

    The lookup exists so that the absence is printed rather than assumed. See
    REPLACEMENTS above for why it is empty and why it stays that way.
    """
    return REPLACEMENTS.get(str(model_id))


def model_verdict(model_id, status, shutdown_date, today):
    """Grade one model id. Pure. Returns (state, detail).

    Distinguishes a date the API stated from a date only the published table
    knows, because those are different levels of evidence and the second one
    goes stale without telling anybody.
    """
    if status is None:
        return ("unreachable", "no response for %s" % model_id)
    status = int(status)
    if status == 404:
        return ("already-gone",
                "%s no longer resolves, so it is out of the model list already"
                % model_id)
    if status != 200:
        return ("unreadable",
                "%d for %s, so nothing can be read about it" % (status, model_id))
    if not shutdown_date:
        return ("no-date-from-api",
                "the model object carried no shutdown_date, so the published "
                "table is the only source and it says %s" % SHUTDOWN)
    left = days_left(today, shutdown_date)
    if left < 0:
        return ("past-shutdown",
                "shutdown_date %s, which was %d day(s) ago"
                % (shutdown_date, -left))
    return ("shutdown-dated",
            "shutdown_date %s, %d day(s) away" % (shutdown_date, left))


def asset_deadline(expires_iso, today, when=SHUTDOWN):
    """The earlier of an asset's two clocks. Pure. (state, deadline, detail).

    The only function here that compares them, and the reason the report is
    sorted by deadline rather than by creation date. A null expiry is not an
    absence of a deadline: it inherits the endpoint's.
    """
    today = str(today)
    when = str(when)
    if not expires_iso:
        return ("no-asset-expiry", when,
                "no expiry of its own, so it dies with the endpoint on %s" % when)
    expires_iso = str(expires_iso)
    if expires_iso <= today:
        return ("already-expired", expires_iso,
                "expired on %s, so the bytes are already unreachable"
                % expires_iso)
    if expires_iso < when:
        gap = days_left(expires_iso, when)
        return ("expires-first", expires_iso,
                "expires %s, which is %d day(s) before the endpoint closes"
                % (expires_iso, gap))
    return ("outlives-the-endpoint", when,
            "its own expiry is %s, so the endpoint closes first on %s"
            % (expires_iso, when))


def spend_verdict(rows, days):
    """Sum the video line items. Pure. Returns (state, total, detail).

    A proxy and labelled as one: neither API lists requests, so spend is the
    only readable measure of how much product is standing on this surface.
    """
    total = 0.0
    for name, amount in rows or []:
        text = str(name or "").lower()
        if "video" in text or "sora" in text:
            total += float(amount or 0)
    if total > 0:
        return ("video-spend-accruing", total,
                "$%.2f on video line items in the last %d day(s), which is a "
                "live feature rather than a branch somebody forgot"
                % (total, days))
    return ("no-video-spend", 0.0,
            "no video line items in the last %d day(s). That is a proxy: it "
            "means nothing was billed, not that nothing calls the endpoint"
            % days)


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed."""
    line = REPAIRS.get(state)
    if not line:
        return []
    if state in ("shutdown-dated", "past-shutdown", "already-gone"):
        return [line,
                "there is no successor model id to print here. The replacement "
                "column is empty for every Sora id in the deprecation table."]
    return [line]


def get_json(session, path, key, params=None, timeout=30):
    """One GET. Returns (status, parsed body). Never raises on a 4xx."""
    try:
        r = session.get(API + path,
                        headers={"Authorization": "Bearer " + key},
                        params=params or {}, timeout=timeout)
    except requests.RequestException as exc:
        log.debug("GET %s failed: %s", path, exc)
        return (None, {})
    try:
        return (r.status_code, r.json())
    except ValueError:
        return (r.status_code, {})


def all_videos(session, key, pages=50):
    """Walk GET /v1/videos to the end. The oldest assets expire first."""
    out, after = [], None
    for _ in range(pages):
        params = {"limit": 100, "order": "asc"}
        if after:
            params["after"] = after
        status, body = get_json(session, "/videos", key, params)
        if status != 200:
            log.warning("video listing came back %s, so the inventory is "
                        "incomplete", status)
            break
        page = body.get("data") or []
        out.extend(page)
        if not page or not body.get("has_more"):
            break
        after = page[-1].get("id")
        if not after:
            break
    return out


def cost_rows(session, key, days):
    """[(line_item, amount)] from the daily cost report."""
    start = int((dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=days)).timestamp())
    status, body = get_json(session, "/organization/costs", key,
                            {"start_time": start, "bucket_width": "1d",
                             "group_by[]": ["line_item"], "limit": 180})
    if status != 200:
        log.warning("cost report came back %s, so the surface was not sized",
                    status)
        return []
    rows = []
    for bucket in body.get("data") or []:
        for row in bucket.get("results") or []:
            amount = row.get("amount") or {}
            rows.append((row.get("line_item"), amount.get("value")))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of cost buckets to read")
    ap.add_argument("--today", default=dt.date.today().isoformat(),
                    help="override the date the arithmetic is done against")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project read key. This script only "
                  "issues GET requests")
        return 2

    session = requests.Session()
    findings = 0
    log.info("endpoint /v1/videos closes %s, %d day(s) left", SHUTDOWN,
             days_left(args.today))

    for model_id in SORA_IDS:
        status, body = get_json(session, "/models/" + model_id, key)
        state, detail = model_verdict(model_id, status,
                                      (body or {}).get("shutdown_date"),
                                      args.today)
        emit = log.warning if state in FINDINGS else log.info
        emit("  %-26s %s  %-15s %s", model_id,
             "---" if status is None else status, state, detail)
        if replacement_for(model_id):
            log.error("  the replacement table is not empty. Read the note "
                      "before trusting this line")
    log.warning("  %-26s the deprecation table lists no successor for any of "
                "these ids, so there is no string to substitute",
                "no-replacement")
    for line in repair_lines("shutdown-dated"):
        log.warning("  repair: %s", line)
    findings += 1

    videos = all_videos(session, key)
    log.info("%d asset(s) in the inventory", len(videos))
    buckets = {}
    for video in videos:
        state, deadline, detail = asset_deadline(
            iso_day(video.get("expires_at")), args.today)
        entry = buckets.setdefault(state, [0, deadline, detail])
        entry[0] += 1
        if deadline < entry[1]:
            entry[1], entry[2] = deadline, detail
    for state, (count, deadline, detail) in sorted(
            buckets.items(), key=lambda kv: kv[1][1]):
        emit = log.warning if state in FINDINGS else log.info
        emit("  %-22s %4d  earliest %s: %s", state, count, deadline, detail)
        for line in repair_lines(state):
            emit("    repair: %s", line)
        if state in FINDINGS:
            findings += 1

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.info("%-22s no admin key, so the surface was not sized",
                 "not-sized")
    else:
        state, total, detail = spend_verdict(cost_rows(session, admin, args.days),
                                             args.days)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-22s %s", state, detail)
        for line in repair_lines(state):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
