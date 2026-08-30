"""Drive every background response id you hold to a terminal status.

Read only. One GET /v1/responses/{response_id} per id and nothing else. This
script does not create a response, does not cancel one and does not retry
anything: cancelling is destructive and retrying costs money, so both are
printed for a human to run.

/v1/responses has no list endpoint. The ids come from your own job table, which
means the audit is bounded by what you wrote down, and the script says so with
every result rather than implying it enumerated anything.

A 404 is graded differently on a zero-data-retention project, where background
responses run unstored and are retained for roughly ten minutes purely so that
polling works at all. Pass --zdr and those stop being reported as lost jobs.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_background_response_audit")

RESPONSES_URL = "https://api.openai.com/v1/responses"

# The documented status enum. Four of the six are not success, and the two that
# look temporary are the ones worth an alarm when they stop being temporary.
OPEN_STATES = ("queued", "in_progress")
TERMINAL_STATES = ("completed", "incomplete", "failed", "cancelled")

# Roughly how long a ZDR project keeps a background response on disk so that it
# can be polled at all. Past this, a 404 there is documented behaviour.
ZDR_WINDOW = 600

BUCKET_ORDER = ("stranded", "failed", "incomplete", "gone", "cancelled",
                "running", "completed", "aged-out", "unreadable")

RETRYABLE = ("server_error", "rate_limit_exceeded")

FINDINGS = ("background-stranded", "background-failed", "background-gone",
            "background-no-ids")


def read_ids(text):
    """[(id, created_hint)] from a file body. Pure. Order kept, ids deduped.

    A line is either an id or "id,<unix timestamp>". The timestamp is what your
    own table recorded at creation, and it is the only way to age a 404, which
    has no object behind it to read a created_at from.
    """
    out, seen = [], set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ident, _, stamp = line.partition(",")
        ident = ident.strip()
        if not ident or ident in seen:
            continue
        seen.add(ident)
        try:
            hint = int(float(stamp.strip())) if stamp.strip() else None
        except ValueError:
            hint = None
        out.append((ident, hint))
    return out


def age_of(response, hint, now):
    """Seconds since creation. Pure. None when neither source has a time."""
    created = (response or {}).get("created_at")
    try:
        created = int(created)
    except (TypeError, ValueError):
        created = None
    if created is None:
        created = hint
    if created is None:
        return None
    return max(0, int(now) - int(created))


def reason_for(response):
    """The failure reason, or "". Pure. Never returns None to be printed."""
    response = response or {}
    error = response.get("error") or {}
    if isinstance(error, dict) and error.get("code"):
        return "error.code %s" % error["code"]
    details = response.get("incomplete_details") or {}
    if isinstance(details, dict) and details.get("reason"):
        return "incomplete_details.reason %s" % details["reason"]
    return ""


def error_code(response):
    """Just the error code, or "". Pure. Used to sort retry from escalate."""
    error = (response or {}).get("error") or {}
    return str(error.get("code") or "") if isinstance(error, dict) else ""


def classify(record, now, sla_seconds, zdr=False):
    """(bucket, detail) for one id. Pure. now and the SLA are arguments.

    The same HTTP 404 is a lost job on an ordinary project and documented
    behaviour on a ZDR one, so the declaration is taken from the caller rather
    than guessed from a field that does not exist.
    """
    http = (record or {}).get("http")
    response = (record or {}).get("response") or {}
    hint = (record or {}).get("created_hint")
    age = age_of(response, hint, now)
    if http == 404:
        if zdr and (age is None or age > ZDR_WINDOW):
            return ("aged-out",
                    "HTTP 404, and on a ZDR project a background response is "
                    "kept only about ten minutes")
        return ("gone", "HTTP 404, no longer retrievable")
    if http != 200:
        return ("unreadable", "HTTP %s" % http)
    status = str(response.get("status") or "")
    if status in OPEN_STATES:
        shown = "%d min" % (age // 60) if age is not None else "an unknown time"
        if age is not None and age > sla_seconds:
            return ("stranded", "%s for %s" % (status, shown))
        return ("running", "%s for %s, inside the service level" % (status, shown))
    if status == "failed":
        return ("failed", reason_for(response) or "failed with no error object")
    if status == "incomplete":
        return ("incomplete", reason_for(response) or "incomplete with no reason")
    if status == "cancelled":
        return ("cancelled", "cancelled")
    if status == "completed":
        return ("completed", "")
    return ("unreadable", "status %r is not one of the six documented values"
            % status)


def summarise(rows):
    """{bucket: count} in a fixed order. Pure. Empty buckets are omitted."""
    counts = {}
    for row in rows or []:
        counts[row.get("bucket")] = counts.get(row.get("bucket"), 0) + 1
    return {b: counts[b] for b in BUCKET_ORDER if b in counts}


def verdict(rows, sla_seconds):
    """Grade the run. Pure. Returns (state, detail)."""
    rows = list(rows or [])
    if not rows:
        return ("background-no-ids",
                "no response ids were supplied. /v1/responses has no list "
                "endpoint, so an empty id file means those jobs are already "
                "unreachable")
    counts = summarise(rows)
    minutes = max(1, int(sla_seconds // 60))
    stranded = counts.get("stranded", 0)
    failed = counts.get("failed", 0)
    gone = counts.get("gone", 0)
    tail = ""
    if failed or gone:
        parts = []
        if failed:
            parts.append("%d failed" % failed)
        if gone:
            parts.append("%d is no longer retrievable" % gone)
        tail = ", " + " and ".join(parts)
    if stranded:
        return ("background-stranded",
                "%d of %d ids have been queued or in_progress past the %d "
                "minute service level%s" % (stranded, len(rows), minutes, tail))
    if failed:
        return ("background-failed",
                "%d of %d ids reached failed and nothing read the error code%s"
                % (failed, len(rows), ", %d is no longer retrievable" % gone
                   if gone else ""))
    if gone:
        return ("background-gone",
                "%d of %d ids no longer resolve, so whatever they produced is "
                "gone" % (gone, len(rows)))
    return ("background-drained",
            "all %d ids are terminal or inside the %d minute service level"
            % (len(rows), minutes))


def repair_lines(state, rows):
    """The repair for one verdict. Pure. Printed, never performed."""
    rows = list(rows or [])
    if state == "background-no-ids":
        return ["persist the response id transactionally with the job row, not "
                "after the call returns. A crash in between leaves a job that "
                "runs, bills, and is referenced nowhere.",
                "there is no list endpoint for /v1/responses, so an id you did "
                "not write down cannot be recovered by any read call."]
    if state == "background-drained":
        return ["nothing stranded. Keep the reconciler running: the failure "
                "mode here is a poller that stops, not one that is wrong."]
    lines = []
    codes = {row.get("code") for row in rows if row.get("code")}
    if codes & set(RETRYABLE):
        lines.append("retry the transient codes (%s), which will usually "
                     "succeed on a second attempt."
                     % ", ".join(sorted(codes & set(RETRYABLE))))
    if codes - set(RETRYABLE):
        lines.append("escalate %s. These fail identically on every attempt, so "
                     "a retry loop only spends money."
                     % ", ".join(sorted(codes - set(RETRYABLE))))
    if any(row.get("bucket") == "stranded" for row in rows):
        lines.append("cancel the stranded jobs you no longer want, at "
                     "/v1/responses/{response_id}/cancel. Only responses "
                     "created with background true can be cancelled, so these "
                     "ones can be.")
    if any(row.get("bucket") == "incomplete" for row in rows):
        lines.append("an incomplete response was cut rather than refused. Read "
                     "incomplete_details.reason: max_output_tokens wants a "
                     "bigger cap, content_filter wants a person.")
    if any(row.get("bucket") == "gone" for row in rows):
        lines.append("an id that no longer resolves cannot be recovered by any "
                     "read call. Archive the output at the moment a response "
                     "reaches completed, not on the next run of a nightly job.")
    return lines


def fetch(response_id, key, timeout=30):
    """One GET. Returns (http_status, payload). Read only, always."""
    try:
        r = requests.get("%s/%s" % (RESPONSES_URL, response_id),
                         headers={"Authorization": "Bearer %s" % key},
                         timeout=timeout)
    except requests.RequestException:
        return (None, {})
    try:
        return (r.status_code, r.json())
    except ValueError:
        return (r.status_code, {})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="file of response ids, one per line, "
                                  "optionally id,<unix created_at>")
    ap.add_argument("--sla-minutes", type=int, default=30,
                    help="age past which queued or in_progress is a finding")
    ap.add_argument("--zdr", action="store_true",
                    help="this project is zero data retention, so a 404 on an "
                         "old background response is documented behaviour")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key set to Read Only")
        return 2

    raw = ""
    if args.ids:
        try:
            with open(args.ids, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            log.error("could not read %s: %s", args.ids, exc)
            return 2
    else:
        raw = os.environ.get("OPENAI_RESPONSE_IDS", "").replace(",", "\n")

    pairs = read_ids(raw)
    now = int(time.time())
    sla = max(1, args.sla_minutes) * 60
    rows = []
    for ident, hint in pairs:
        http, payload = fetch(ident, key)
        bucket, detail = classify({"http": http, "response": payload,
                                   "created_hint": hint}, now, sla, args.zdr)
        rows.append({"id": ident, "bucket": bucket, "detail": detail,
                     "code": error_code(payload)})
        emit = log.warning if bucket in ("stranded", "failed", "gone") else log.info
        emit("%-16s %-12s %s", ident[:16], bucket, detail)

    state, detail = verdict(rows, sla)
    emit = log.warning if state in FINDINGS else log.info
    emit("%-20s %s", state, detail)
    counts = summarise(rows)
    if counts:
        emit("  buckets: %s", ", ".join("%s %d" % (k, v) for k, v in counts.items()))
    emit("  measured: status, error.code and incomplete_details.reason from one "
         "GET per id")
    emit("  inferred: nothing about ids not in the file, because /v1/responses "
         "has no list endpoint and cannot be enumerated")
    for line in repair_lines(state, rows):
        emit("  repair: %s", line)

    findings = sum(counts.get(b, 0) for b in ("stranded", "failed", "gone"))
    if state == "background-no-ids":
        findings = 1
    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
