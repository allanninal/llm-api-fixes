"""Check the file ids an application holds against the expiry on each one.

Read only. GET /v1/files with an ids[] filter and nothing else. No file content
is ever downloaded, nothing is uploaded and nothing is deleted.

expires_in_seconds is set once at upload, between 3600 and 7776000 seconds, and
cannot be changed afterwards. After expires_at the content stops being
retrievable and the bytes leave the storage quota, but the metadata remains
readable for up to 30 days and the file keeps appearing in list responses. So
an existence check answers yes for a file that fails every real use.

The ids form accepts at most 100 values after de-duplication, is mutually
exclusive with page and limit, and always returns a single page. Ids that do
not resolve are silently omitted from data, which is read here as a result
rather than as an error.

No anthropic-beta header is sent. With files-api-2025-04-14 the response omits
expires_at entirely, and a run that cannot see the field says so instead of
reporting that nothing is expiring.
"""
import argparse
import calendar
import logging
import os
import re
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_expired_file_refs")

BASE_URL = "https://api.anthropic.com/v1/files"

# Documented: at most 100 ids per request, after de-duplication.
ID_BATCH = 100
# Documented: metadata stays readable for up to 30 days past expires_at.
METADATA_WINDOW_DAYS = 30

FINDINGS = ("expired", "expiring", "gone", "expiry-not-reported")

_RFC3339 = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})"
                      r"(?:\.\d+)?(Z|z|[+-]\d{2}:?\d{2})?$")


def parse_ids(text):
    """File ids from an export nobody tidied. Pure. Blanks and repeats dropped."""
    seen = []
    for line in str(text or "").splitlines():
        item = line.split("#", 1)[0].strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def chunks(ids, size=ID_BATCH):
    """Batches of at most `size` unique ids. Pure.

    The cap is not a performance choice. The ids form is documented at 100
    values after de-duplication and is mutually exclusive with page and limit,
    so a longer list is a different request rather than a slower one.
    """
    try:
        size = max(1, min(int(size), ID_BATCH))
    except (TypeError, ValueError):
        size = ID_BATCH
    unique, out = [], []
    for item in ids or []:
        item = str(item or "").strip()
        if item and item not in unique:
            unique.append(item)
    for i in range(0, len(unique), size):
        out.append(unique[i:i + size])
    return out


def epoch(value):
    """RFC 3339 to seconds. Pure. Zero for anything unparseable, never a guess."""
    if value is None or value == "" or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0
    m = _RFC3339.match(str(value).strip())
    if not m:
        return 0
    try:
        base = calendar.timegm(tuple(int(g) for g in m.groups()[:6]) + (0, 0, 0))
    except (TypeError, ValueError):
        return 0
    off = m.group(7)
    if off and off not in ("Z", "z"):
        digits = off[1:].replace(":", "")
        shift = int(digits[:2]) * 3600 + int(digits[2:4]) * 60
        base -= shift if off[0] == "+" else -shift
    return max(0, base)


def file_row(body):
    """One file object, reduced. Pure.

    `expiry_reported` records whether the key was present at all, which is a
    different fact from the key being null. Absent means the response shape
    does not carry expiry and this check cannot run; null means the file was
    uploaded without one and is permanent.
    """
    body = body if isinstance(body, dict) else {}
    try:
        size = int(body.get("size_bytes"))
    except (TypeError, ValueError):
        size = 0
    return {"id": str(body.get("id") or ""),
            "filename": str(body.get("filename") or ""),
            "size": max(0, size),
            "created_at": epoch(body.get("created_at")),
            "expires_at": epoch(body.get("expires_at")) or None,
            "expiry_reported": "expires_at" in body,
            "downloadable": bool(body.get("downloadable"))}


def missing_ids(requested, returned):
    """Ids asked for and not answered. Pure. Order preserved.

    Unresolvable ids are silently omitted from data with no error and no
    marker, so this diff is the only way the omission becomes a result.
    """
    have = {str(r or "") for r in returned or []}
    return [str(r) for r in requested or [] if str(r) not in have]


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


def classify_id(row, now, warn_days):
    """Grade one referenced id. Pure. Returns (state, detail).

    `row` is None for an id the API declined to return at all.
    """
    if row is None:
        return ("gone", "not returned by the ids lookup, so it is past even "
                        "the %d day metadata window or was deleted"
                        % METADATA_WINDOW_DAYS)
    if not row.get("expiry_reported"):
        return ("expiry-not-reported",
                "the object came back with no expires_at field, so this check "
                "could not run")
    expires = row.get("expires_at")
    if not expires:
        return ("no-expiry", "no expiry was set, so this one is permanent")
    left = (int(expires) - int(now)) / 86400.0
    if left <= 0:
        return ("expired",
                "expired %.1f day(s) ago; the metadata still answers and every "
                "actual use of this id fails" % abs(left))
    if left <= float(warn_days):
        return ("expiring",
                "expires in %.1f day(s), and the expiry cannot be extended" % left)
    return ("live", "live, expires in %.1f day(s)" % left)


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state == "expired":
        return ["the content is gone and cannot be restored. Remove the "
                "reference, re-upload the source if you still need it, and "
                "DELETE /v1/files/{file_id} to clear the metadata immediately "
                "rather than waiting out the %d day window."
                % METADATA_WINDOW_DAYS]
    if state == "expiring":
        return ["expires_in_seconds is set once at upload and cannot be "
                "changed, so there is nothing to extend. Re-upload before the "
                "date and swap the id, or upload with no expiry and accept "
                "that it stays on the storage quota."]
    if state == "gone":
        return ["this id resolves to nothing at all. Treat the record as stale "
                "and stop passing it, because no read will recover the file."]
    if state == "expiry-not-reported":
        return ["drop the anthropic-beta: files-api-2025-04-14 header. With it "
                "the response omits expires_at entirely and reverts to "
                "before_id and after_id paging, so this check cannot run."]
    if state == "no-expiry":
        return ["nothing to do here, but note that a file with no expiry never "
                "leaves the storage total either."]
    return []


def fetch_batch(batch, key, timeout=30):
    """One GET with an ids[] filter. Returns (rows, ok).

    No limit and no page: both are mutually exclusive with ids, and the ids
    form always returns a single page. No beta header either, because with one
    the response would not carry expires_at at all.
    """
    params = [("ids[]", fid) for fid in batch]
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    try:
        r = requests.get(BASE_URL, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        log.error("ids lookup failed: %s", exc)
        return ([], False)
    if r.status_code != 200:
        log.error("ids lookup returned HTTP %s", r.status_code)
        return ([], False)
    try:
        body = r.json()
    except ValueError:
        return ([], False)
    return ([file_row(item) for item in (body.get("data") or [])], True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", required=True,
                    help="file of file ids your application references")
    ap.add_argument("--warn-days", type=float, default=7.0,
                    help="days of remaining life that count as a finding")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a key with access to the workspace "
                  "that owns these files. Every call is a GET of /v1/files")
        return 2
    try:
        with open(args.ids, "r", encoding="utf-8") as fh:
            wanted = parse_ids(fh.read())
    except OSError as exc:
        log.error("could not read %s: %s", args.ids, exc)
        return 2
    if not wanted:
        log.error("no file ids in %s. This note is about the ids your "
                  "application holds, not about the workspace listing", args.ids)
        return 2

    now = int(time.time())
    batches = chunks(wanted)
    rows, missing = [], []
    for batch in batches:
        got, ok = fetch_batch(batch, key)
        if not ok:
            log.error("a batch could not be read, so nothing is concluded")
            return 2
        rows.extend(got)
        missing.extend(missing_ids(batch, [r["id"] for r in got]))

    log.info("%d id(s) asked in %d batch(es) of at most %d, %d returned",
             len(wanted), len(batches), ID_BATCH, len(rows))

    findings = 0
    for row in rows:
        state, detail = classify_id(row, now, args.warn_days)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-20s %s: %s", state, row["id"], detail)
        for line in repair_lines(state):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1
    for fid in missing:
        state, detail = classify_id(None, now, args.warn_days)
        log.warning("%-20s %s: %s", state, fid, detail)
        for line in repair_lines(state):
            log.warning("  repair: %s", line)
        findings += 1

    log.info("%d id(s) missing from the response, %d finding(s)",
             len(missing), findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
