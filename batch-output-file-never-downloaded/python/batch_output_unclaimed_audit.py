"""Find batch results that were paid for and never collected, on both providers.

Read only. Three GET endpoints: /v1/batches and /v1/files on OpenAI,
/v1/messages/batches on Anthropic. Nothing is downloaded, deleted or re-run.

Neither API records whether you read a result. File objects have no
last_accessed_at, batches have no consumed flag, and Anthropic's archived_at
means the results are gone rather than that they were taken. So the ledger of
what your consumer has processed is an input, and an empty one is a verdict.

The clocks are anchored differently and it matters. An OpenAI batch output file
is deleted 30 days after the batch is complete; where the file object carries
its own expires_at, that is authoritative and is used instead. Claude batch
results are available for 29 days after the batch was created, not after it
ended.

This is the mirror of the error-file note. That one reads error_file_id, the
list of rows that failed. This one reads output_file_id and results_url, which
are the work itself.
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
log = logging.getLogger("batch_output_unclaimed_audit")

OPENAI_BATCHES_URL = "https://api.openai.com/v1/batches"
OPENAI_FILES_URL = "https://api.openai.com/v1/files"
ANTHROPIC_BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"

# 30 days from completion on OpenAI, 29 days from creation on Anthropic. The
# anchors are different and sorting by the wrong one puts the queue backwards.
OPENAI_RETENTION = 30 * 86400
ANTHROPIC_RETENTION = 29 * 86400

# Both providers cap batch processing at 24 hours. Past that plus a little
# slack, a non-terminal batch is a stale object rather than a slow job.
OPEN_WINDOW = 24 * 3600
GRACE = 2 * 3600

OPENAI_TERMINAL = ("completed", "failed", "expired", "cancelled")

FINDINGS = ("batch-output-expiring", "batch-output-lost",
            "batch-output-unclaimed", "batch-never-polled")


def read_ledger(text):
    """Set of batch ids your consumer has processed. Pure. Comments dropped."""
    out = set()
    for raw in (text or "").replace(",", "\n").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def parse_time(value):
    """Epoch seconds from a unix number or an RFC 3339 string. Pure.

    A string with no offset is read as UTC rather than as whatever the machine
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


def file_index(files):
    """{file_id: file object}. Pure. One index instead of a call per batch."""
    out = {}
    for f in files or []:
        if isinstance(f, dict) and f.get("id"):
            out[str(f["id"])] = f
    return out


def openai_deadline(batch, file_obj):
    """(epoch, source) for when this output disappears. Pure.

    The file's own expires_at is the platform speaking. Everything else is
    arithmetic on the documented 30 days from completion, and the source is
    reported so nobody argues with a number whose provenance is invisible.
    """
    stamp = parse_time((file_obj or {}).get("expires_at"))
    if stamp:
        return (stamp, "expires_at")
    completed = parse_time((batch or {}).get("completed_at"))
    if completed:
        return (completed + OPENAI_RETENTION, "completed_at + 30d")
    created = parse_time((batch or {}).get("created_at"))
    if created:
        return (created + OPENAI_RETENTION, "created_at + 30d")
    return (None, "unknown")


def days_left(deadline, now):
    """Whole days until the deadline. Pure. None when there is no deadline."""
    if deadline is None:
        return None
    return int((deadline - now) // 86400)


def openai_rows(batches, index, ledger, now, warn_days):
    """One row per OpenAI batch worth reporting. Pure."""
    rows = []
    for b in batches or []:
        status = str((b or {}).get("status") or "")
        ident = str(b.get("id"))
        created = parse_time(b.get("created_at"))
        if status not in OPENAI_TERMINAL:
            if created is not None and now - created > OPEN_WINDOW + GRACE:
                rows.append({"provider": "openai", "id": ident, "state": "stalled",
                             "done": 0, "days": None,
                             "detail": "%s for %d h, past the 24 h window"
                                       % (status, (now - created) // 3600)})
            continue
        if status != "completed":
            continue
        counts = b.get("request_counts") or {}
        done = int(counts.get("completed") or 0)
        artifact = b.get("output_file_id")
        if not artifact:
            continue
        if str(artifact) not in (index or {}):
            rows.append({"provider": "openai", "id": ident, "state": "lost",
                         "done": done, "days": None,
                         "detail": "output_file_id %s no longer exists" % artifact})
            continue
        deadline, source = openai_deadline(b, index[str(artifact)])
        left = days_left(deadline, now)
        if left is not None and left <= warn_days:
            state = "expiring"
            detail = "%d completed, %d days left (%s)" % (done, max(0, left), source)
        elif ident in (ledger or set()):
            state = "claimed"
            detail = "%d completed, in the ingest ledger" % done
        else:
            state = "unclaimed"
            detail = "%d completed, %s days left" % (
                done, "unknown" if left is None else max(0, left))
        rows.append({"provider": "openai", "id": ident, "state": state,
                     "done": done, "days": left, "detail": detail})
    return rows


def anthropic_rows(batches, ledger, now, warn_days):
    """One row per Claude batch worth reporting. Pure."""
    rows = []
    for b in batches or []:
        ident = str((b or {}).get("id"))
        status = str(b.get("processing_status") or "")
        created = parse_time(b.get("created_at"))
        counts = b.get("request_counts") or {}
        done = int(counts.get("succeeded") or 0)
        if status != "ended":
            if created is not None and now - created > OPEN_WINDOW + GRACE:
                rows.append({"provider": "anthropic", "id": ident,
                             "state": "stalled", "done": done, "days": None,
                             "detail": "%s for %d h, past the 24 h window"
                                       % (status or "unknown",
                                          (now - created) // 3600)})
            continue
        if done <= 0:
            continue
        if b.get("archived_at"):
            rows.append({"provider": "anthropic", "id": ident, "state": "lost",
                         "done": done, "days": None,
                         "detail": "archived_at set, %d succeeded, gone" % done})
            continue
        left = days_left(created + ANTHROPIC_RETENTION, now) \
            if created is not None else None
        if left is not None and left <= warn_days:
            state = "expiring"
            detail = "%d succeeded, %d days left (created_at + 29d)" % (
                done, max(0, left))
        elif ident in (ledger or set()):
            state = "claimed"
            detail = "%d succeeded, in the ingest ledger" % done
        else:
            state = "unclaimed"
            detail = "%d succeeded, %s days left" % (
                done, "unknown" if left is None else max(0, left))
        rows.append({"provider": "anthropic", "id": ident, "state": state,
                     "done": done, "days": left, "detail": detail})
    return rows


def by_urgency(rows):
    """Rows ordered by what you can still act on. Pure. Stable within a state."""
    rank = {"expiring": 0, "lost": 1, "unclaimed": 2, "stalled": 3, "claimed": 4}
    return sorted(rows or [], key=lambda r: (rank.get(r.get("state"), 9),
                                             99999 if r.get("days") is None
                                             else r["days"], r.get("id") or ""))


def counts_by_state(rows):
    """{state: n}. Pure."""
    out = {}
    for row in rows or []:
        out[row.get("state")] = out.get(row.get("state"), 0) + 1
    return out


def verdict(rows, ledger, warn_days):
    """Grade the run. Pure. Returns (state, detail).

    Ranked by whether you can still do something about it. An expiring result
    is the only category with a deadline you can beat, so it wins even when the
    unclaimed pile is larger.
    """
    rows = list(rows or [])
    if not rows:
        return ("batch-output-clean",
                "every batch on the providers checked is either open inside its "
                "window or terminal with its output accounted for")
    c = counts_by_state(rows)
    parts = []
    if c.get("lost"):
        parts.append("%d are already unrecoverable" % c["lost"])
    if c.get("unclaimed"):
        parts.append("%d were never claimed" % c["unclaimed"])
    if c.get("stalled"):
        parts.append("%d never reached a terminal state" % c["stalled"])
    tail = (", " + ", ".join(parts)) if parts else ""
    if c.get("expiring"):
        return ("batch-output-expiring",
                "%d batch(es) hold results that expire within %d days%s"
                % (c["expiring"], warn_days, tail))
    if c.get("lost"):
        return ("batch-output-lost",
                "%d batch(es) hold results that are already gone and can only "
                "be recovered by re-running them%s"
                % (c["lost"], (", " + ", ".join(parts[1:])) if parts[1:] else ""))
    if c.get("unclaimed"):
        detail = ("%d batch(es) ended with results nothing has collected"
                  % c["unclaimed"])
        if not ledger:
            detail += (", and no ingest ledger was supplied, so every terminal "
                       "batch counts as unclaimed")
        return ("batch-output-unclaimed", detail)
    if c.get("stalled"):
        return ("batch-never-polled",
                "%d batch(es) have been open longer than the 24 hour window, "
                "which means nothing has polled them" % c["stalled"])
    return ("batch-output-clean",
            "all %d terminal batch(es) are in the ingest ledger with runway "
            "left on the clock" % len(rows))


def repair_lines(state, rows, ledger):
    """The repair for one verdict. Pure. Printed, never performed."""
    rows = list(rows or [])
    c = counts_by_state(rows)
    if state == "batch-output-clean":
        return ["nothing outstanding. Keep the assertion that a batch is not "
                "done until its output has been archived into your own store."]
    lines = []
    if c.get("expiring"):
        lines.append("download the expiring outputs today and persist them "
                     "keyed by batch id. After the clock runs out no read call "
                     "recovers them.")
    if c.get("lost"):
        lines.append("the lost ones must be re-run and re-paid. Nothing in "
                     "either API can return results after the retention window "
                     "closes.")
    if c.get("unclaimed"):
        lines.append("sweep the unclaimed batches: list, diff against your "
                     "ledger, download, and key the rows by custom_id, which "
                     "is the only join available since results are not "
                     "returned in request order.")
    if c.get("stalled"):
        lines.append("a batch open past 24 hours is a stale object rather than "
                     "a slow job. Poll every id you create to a terminal state, "
                     "and record the id at creation time so orphans are "
                     "identifiable.")
    if not ledger:
        lines.append("no ingest ledger was supplied, so nothing could be "
                     "confirmed as consumed. Record every batch id your "
                     "consumer processes: neither API offers a read receipt.")
    lines.append("run the error-file audit alongside this one. That note reads "
                 "error_file_id, the list of rows that failed; this one reads "
                 "the work itself. Both assertions belong in the same batch "
                 "completion handler.")
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


def page(url, headers, params, max_pages, cursor="after"):
    """Follow a cursor. Returns (rows, error). GETs only."""
    rows, token = [], None
    for _ in range(max(1, max_pages)):
        query = dict(params or {})
        if token:
            query[cursor] = token
        payload, err = get_json(url, headers, query)
        if err:
            return (rows, err)
        data = payload.get("data") or []
        rows.extend(data)
        if not payload.get("has_more") or not data:
            break
        token = payload.get("last_id") or data[-1].get("id")
        if not token:
            break
    return (rows, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", help="file of batch ids your consumer processed")
    ap.add_argument("--warn-days", type=int, default=5,
                    help="days of runway below which a result counts as expiring")
    ap.add_argument("--max-pages", type=int, default=20)
    args = ap.parse_args()

    openai_key = os.environ.get("OPENAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not openai_key and not anthropic_key:
        log.error("set OPENAI_API_KEY (project key, Read Only) or "
                  "ANTHROPIC_API_KEY (workspace key), or both")
        return 2

    raw = os.environ.get("BATCH_INGEST_LEDGER", "")
    if args.ledger:
        try:
            with open(args.ledger, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            log.error("could not read %s: %s", args.ledger, exc)
            return 2
    ledger = read_ledger(raw)

    now = int(time.time())
    rows, checked = [], []
    if openai_key:
        checked.append("openai")
        headers = {"Authorization": "Bearer %s" % openai_key}
        batches, err = page(OPENAI_BATCHES_URL, headers, {"limit": 100},
                            args.max_pages)
        if err:
            log.warning("openai batch list stopped early: %s", err)
        files, ferr = page(OPENAI_FILES_URL, headers,
                           {"limit": 10000, "purpose": "batch_output"},
                           args.max_pages)
        if ferr:
            log.warning("openai file list stopped early: %s", ferr)
        rows.extend(openai_rows(batches, file_index(files), ledger, now,
                                args.warn_days))
    if anthropic_key:
        checked.append("anthropic")
        headers = {"x-api-key": anthropic_key, "anthropic-version": "2023-06-01"}
        batches, err = page(ANTHROPIC_BATCHES_URL, headers, {"limit": 1000},
                            args.max_pages, cursor="after_id")
        if err:
            log.warning("anthropic batch list stopped early: %s", err)
        rows.extend(anthropic_rows(batches, ledger, now, args.warn_days))

    reportable = [r for r in rows if r["state"] != "claimed"]
    for row in by_urgency(reportable):
        log.warning("%-10s %-14s %-12s %s", row["provider"], row["id"][:14],
                    row["state"], row["detail"])

    state, detail = verdict(reportable, ledger, args.warn_days)
    emit = log.warning if state in FINDINGS else log.info
    emit("%-22s %s", state, detail)
    emit("  checked: %s, %d batch id(s) in the ledger",
         ", ".join(checked) or "nothing", len(ledger))
    emit("  measured: status, the result artifact and the retention clock from "
         "the batch lists, and file existence from the file list")
    emit("  inferred: that an id absent from your ledger was never consumed, "
         "since neither API records whether a result was downloaded")
    for line in repair_lines(state, reportable, ledger):
        emit("  repair: %s", line)

    log.info("%d finding(s)", len(reportable))
    return 1 if reportable else 0


if __name__ == "__main__":
    sys.exit(main())
