"""Find billed, salvageable output left behind by cancelled batches.

Read only, on both providers. Two GET endpoints, /v1/batches on OpenAI and
/v1/messages/batches on Anthropic. This script never cancels a batch, never
submits one, and never downloads a result file.

Cancel is a stop, not a rollback. Requests that reached the model before the
cancel landed are finished and are in the output; re-running the whole batch
pays for them twice. Anthropic documents that canceled and expired requests are
not billed. OpenAI documents the partial output but not the billing split, so
the completed count is reported as a floor rather than as a total.

Retention of that partial output, and the join against your own ingest ledger,
belong to the unclaimed-output note. This one stops at the arithmetic.
"""
import argparse
import calendar
import datetime
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("batch_cancellation_audit")

OPENAI_BATCHES_URL = "https://api.openai.com/v1/batches"
ANTHROPIC_BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"

# OpenAI documents up to ten minutes in "cancelling" before a batch reaches
# "cancelled". Fifteen is a generous read of ten. Anthropic publishes no bound
# for "canceling" at all, so on that side this is a heuristic and the output
# says so rather than dressing a guess up as a rule.
STUCK_SECONDS = 15 * 60

OPENAI_CANCEL_STATES = ("cancelling", "cancelled")

FINDINGS = ("cancel-stuck", "cancel-partial-unclaimed")


def parse_time(value):
    """Epoch seconds from a unix number or an RFC 3339 string. Pure.

    OpenAI stamps integers, Anthropic stamps RFC 3339. Everything downstream
    wants one kind of number, so the difference is absorbed once, here. A
    string with no offset is read as UTC rather than as whatever the machine
    running the audit happens to be set to.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        return calendar.timegm(stamp.timetuple())
    return int(stamp.timestamp())


def openai_cancel_rows(batches):
    """Normalised rows for OpenAI batches under cancellation. Pure."""
    out = []
    for b in batches or []:
        status = (b or {}).get("status")
        if status not in OPENAI_CANCEL_STATES:
            continue
        counts = b.get("request_counts") or {}
        total = int(counts.get("total") or 0)
        done = int(counts.get("completed") or 0)
        failed = int(counts.get("failed") or 0)
        out.append({
            "provider": "openai",
            "id": str(b.get("id")),
            "status": status,
            "in_flight": status == "cancelling",
            "done": done,
            "stopped": max(0, total - done - failed),
            "total": total,
            "artifact": b.get("output_file_id"),
            "cancel_started": parse_time(b.get("cancelling_at")),
            "billing_known": False,
        })
    return sorted(out, key=lambda r: r["id"])


def anthropic_cancel_rows(batches):
    """Normalised rows for Claude batches under cancellation. Pure.

    cancel_initiated_at is set only when cancellation was initiated, and stays
    set once the batch has ended, so it is the whole filter.
    """
    out = []
    for b in batches or []:
        started = (b or {}).get("cancel_initiated_at")
        if not started:
            continue
        counts = b.get("request_counts") or {}
        done = int(counts.get("succeeded") or 0)
        stopped = int(counts.get("canceled") or 0)
        status = str(b.get("processing_status") or "")
        out.append({
            "provider": "anthropic",
            "id": str(b.get("id")),
            "status": status or "unknown",
            "in_flight": status == "canceling",
            "done": done,
            "stopped": stopped,
            "total": done + stopped + int(counts.get("errored") or 0)
                     + int(counts.get("expired") or 0)
                     + int(counts.get("processing") or 0),
            "artifact": b.get("results_url"),
            "cancel_started": parse_time(started),
            "billing_known": True,
        })
    return sorted(out, key=lambda r: r["id"])


def salvage_rows(rows):
    """Rows holding finished work a re-run would pay for again. Pure."""
    return [r for r in rows or [] if int(r.get("done") or 0) > 0]


def stuck_rows(rows, now, seconds=STUCK_SECONDS):
    """Rows still mid cancel past the threshold. Pure. now is an argument."""
    out = []
    for r in rows or []:
        if not r.get("in_flight"):
            continue
        started = r.get("cancel_started")
        if started is None or now - started > seconds:
            out.append(r)
    return out


def salvaged_total(rows):
    """Finished rows across everything cancelled. Pure."""
    return sum(int(r.get("done") or 0) for r in salvage_rows(rows))


def verdict(rows, stuck, salvage):
    """Grade the run. Pure. Returns (state, detail)."""
    rows = list(rows or [])
    stuck = list(stuck or [])
    salvage = list(salvage or [])
    if not rows:
        return ("no-cancels",
                "no batch on the providers checked has had a cancellation "
                "initiated")
    if stuck:
        detail = ("%d batch(es) have been mid cancel longer than the documented "
                  "window" % len(stuck))
        if salvage:
            detail += (", and %d cancelled batch(es) hold %d finished rows "
                       "nothing has collected"
                       % (len(salvage), salvaged_total(salvage)))
        return ("cancel-stuck", detail)
    if salvage:
        return ("cancel-partial-unclaimed",
                "%d cancelled batch(es) hold %d finished rows a re-run would "
                "pay for again" % (len(salvage), salvaged_total(salvage)))
    return ("cancel-clean",
            "%d cancellation(s) found, none of which had completed a single "
            "request, so there is nothing to salvage and nothing to double pay"
            % len(rows))


def repair_lines(state, rows):
    """The repair for one verdict. Pure. Printed, never performed."""
    rows = list(rows or [])
    if state == "no-cancels":
        return []
    if state == "cancel-clean":
        return ["nothing to collect. Keep cancelling early: a batch stopped "
                "before its first request completed costs nothing."]
    lines = []
    if state == "cancel-stuck":
        lines.append("a batch still in cancelling or canceling has not stopped. "
                     "Poll it to a terminal state before you submit a "
                     "replacement, or the two will run the same rows at once.")
    if any(int(r.get("done") or 0) > 0 for r in rows):
        lines.append("download the partial output, drop those custom_ids from "
                     "the input file, and submit only the remainder. Results "
                     "are not returned in request order, so custom_id is the "
                     "only join key that works.")
    if any(r.get("provider") == "anthropic" and int(r.get("done") or 0) > 0
           for r in rows):
        lines.append("on Anthropic, canceled and expired requests are not "
                     "billed, so the succeeded count is the whole cost of the "
                     "cancelled batch.")
    if any(r.get("provider") == "openai" and int(r.get("done") or 0) > 0
           for r in rows):
        lines.append("on OpenAI the billing split for a cancelled batch is not "
                     "documented, so treat the completed count as a floor and "
                     "confirm the day against the cost report.")
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


def page_openai(key, max_pages):
    """/v1/batches, following the after cursor. GETs only."""
    rows, after = [], None
    headers = {"Authorization": "Bearer %s" % key}
    for _ in range(max(1, max_pages)):
        params = {"limit": 100}
        if after:
            params["after"] = after
        payload, err = get_json(OPENAI_BATCHES_URL, headers, params)
        if err:
            return (rows, err)
        data = payload.get("data") or []
        rows.extend(data)
        if not payload.get("has_more") or not data:
            break
        after = data[-1].get("id")
    return (rows, None)


def page_anthropic(key, max_pages):
    """/v1/messages/batches, following after_id. GETs only."""
    rows, after = [], None
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    for _ in range(max(1, max_pages)):
        params = {"limit": 1000}
        if after:
            params["after_id"] = after
        payload, err = get_json(ANTHROPIC_BATCHES_URL, headers, params)
        if err:
            return (rows, err)
        data = payload.get("data") or []
        rows.extend(data)
        if not payload.get("has_more") or not data:
            break
        after = payload.get("last_id") or data[-1].get("id")
    return (rows, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stuck-minutes", type=int, default=15,
                    help="age past which a batch still mid cancel is reported")
    ap.add_argument("--max-pages", type=int, default=20)
    args = ap.parse_args()

    openai_key = os.environ.get("OPENAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not openai_key and not anthropic_key:
        log.error("set OPENAI_API_KEY (project key, Read Only) or "
                  "ANTHROPIC_API_KEY (workspace key), or both")
        return 2

    now = int(time.time())
    rows = []
    checked = []
    if openai_key:
        checked.append("openai")
        batches, err = page_openai(openai_key, args.max_pages)
        if err:
            log.warning("openai batch list stopped early: %s", err)
        rows.extend(openai_cancel_rows(batches))
    if anthropic_key:
        checked.append("anthropic")
        batches, err = page_anthropic(anthropic_key, args.max_pages)
        if err:
            log.warning("anthropic batch list stopped early: %s", err)
        rows.extend(anthropic_cancel_rows(batches))

    stuck = stuck_rows(rows, now, max(1, args.stuck_minutes) * 60)
    salvage = salvage_rows(rows)
    stuck_ids = {r["id"] for r in stuck}

    for r in rows:
        log.info("%-11s %-14s %-11s %s of %s done, %s stopped", r["provider"],
                 r["id"][:14], r["status"], format(r["done"], ","),
                 format(r["total"], ","), format(r["stopped"], ","))
        if r["artifact"]:
            label = "output_file_id" if r["provider"] == "openai" else "results_url"
            log.info("%-11s %-14s   %s present", "", "", label)
        if r["id"] in stuck_ids:
            started = r.get("cancel_started")
            age = "an unknown time" if started is None \
                else "%d min" % ((now - started) // 60)
            log.warning("%-11s %-14s   mid cancel for %s", r["provider"],
                        r["id"][:14], age)

    state, detail = verdict(rows, stuck, salvage)
    emit = log.warning if state in FINDINGS else log.info
    emit("%-20s %s", state, detail)
    emit("  checked: %s", ", ".join(checked))
    emit("  measured: request_counts and the cancellation timestamps from the "
         "batch lists")
    emit("  inferred: that a re-run would repeat the finished rows, since "
         "neither API records whether the partial output was downloaded")
    for line in repair_lines(state, rows):
        emit("  repair: %s", line)

    total = len(stuck) + len(salvage)
    log.info("%d finding(s)", total)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
