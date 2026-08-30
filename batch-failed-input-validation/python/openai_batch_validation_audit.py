"""Report OpenAI batches that failed input validation, and the lines that broke.

Read only. Two GET endpoints, /v1/batches and /v1/files, and nothing else. No
file is uploaded, no batch is created, and no failed batch is re-submitted:
re-running rows spends money on inference and only you know whether the rows
are still wanted.

On the Batch API, status "failed" is a specific claim. It means the input file
did not survive validation, which happens before any request reaches the model.
So request_counts is all zeros and nothing was billed. Individual requests that
failed inside a batch that ran are a different status and a different note.

The second half reads the file list, because an input file uploaded under the
wrong purpose is rejected at creation and never becomes a batch object at all.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_batch_validation_audit")

BATCHES_URL = "https://api.openai.com/v1/batches"
FILES_URL = "https://api.openai.com/v1/files"

# The batch list takes limit (1-100) and after. There is no status filter and no
# date range, so every bit of selection below happens on this side of the wire.
PAGE = 100

# Purposes the Files API namespaces separately from batch input. A .jsonl parked
# under one of these was uploaded for a batch that /v1/batches refused, since it
# accepts an input file only when purpose is exactly "batch".
NOT_BATCH_INPUT = ("user_data", "assistants", "fine-tune", "vision")

# Error codes the docs and the endpoint's own validation produce often enough to
# be worth a specific repair line rather than the generic one.
KNOWN_CODES = {
    "invalid_json": "a line is not valid JSON. Validate the file locally before "
                    "upload: every line must parse on its own.",
    "duplicate_custom_id": "two lines share a custom_id. They must be unique "
                           "within the file, because results come back unordered "
                           "and custom_id is the only join key.",
    "missing_required_parameter": "a line is missing a required field. Each row "
                                  "needs custom_id, method, url and body.",
    "invalid_url": "a line's url does not match the batch endpoint. The two must "
                   "agree for every row in the file.",
    "model_not_found": "the body names a model this project cannot reach. Check "
                       "the id against GET /v1/models with the same key.",
    "empty_file": "the input file has no lines in it. The upload succeeded and "
                  "the content did not.",
}

FINDINGS = ("validation-failed", "orphan-input-files")


def failed_batches(batches):
    """Batches whose input file was rejected. Pure.

    The only status that means "validation refused this file". A batch that ran
    and had rows fail inside it reports completed with a non-zero
    request_counts.failed, which this script deliberately does not look at.
    """
    return [b for b in batches or [] if (b or {}).get("status") == "failed"]


def error_rows(batch):
    """Normalised entries from errors.data[]. Pure. Never raises on a shape.

    errors is optional, its data list is optional, and an entry's line is
    documented as "if applicable", so every field here is allowed to be absent.
    """
    errors = (batch or {}).get("errors") or {}
    data = errors.get("data") if isinstance(errors, dict) else None
    out = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        try:
            line = int(line)
        except (TypeError, ValueError):
            line = None
        out.append({"code": str(item.get("code") or "unknown"),
                    "message": str(item.get("message") or ""),
                    "param": item.get("param"),
                    "line": line})
    return out


def lines_by_code(rows):
    """{code: (sorted lines, count, one message, one param)}. Pure.

    A file with 40,000 bad rows produces 40,000 near-identical messages. One row
    per code carrying the line numbers is the same information and is readable.
    """
    grouped = {}
    for row in rows or []:
        slot = grouped.setdefault(row["code"], {"lines": set(), "count": 0,
                                                "message": "", "param": None})
        slot["count"] += 1
        if row.get("line") is not None:
            slot["lines"].add(row["line"])
        if not slot["message"]:
            slot["message"] = row.get("message") or ""
        if slot["param"] is None:
            slot["param"] = row.get("param")
    return {code: (sorted(v["lines"]), v["count"], v["message"], v["param"])
            for code, v in sorted(grouped.items())}


def nothing_billed(batch):
    """True when the batch dispatched no requests at all. Pure.

    Validation runs before dispatch, so a failed batch has an all-zero
    request_counts. An absent counts object is treated as zero, which is what a
    batch that never started actually looks like.
    """
    counts = (batch or {}).get("request_counts") or {}
    try:
        return all(int(counts.get(k) or 0) == 0
                   for k in ("total", "completed", "failed"))
    except (TypeError, ValueError):
        return False


def batch_input_ids(batches):
    """Every input_file_id the account has ever handed to a batch. Pure."""
    return {str(b.get("input_file_id")) for b in batches or []
            if (b or {}).get("input_file_id")}


def mispurposed_inputs(files, used_ids):
    """.jsonl files that can never be batch input and never were. Pure.

    All three conditions matter. Drop the last one and every input file you ever
    used successfully gets flagged, which is how a check gets switched off.
    """
    out = []
    for f in files or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("filename") or "")
        purpose = str(f.get("purpose") or "")
        if not name.lower().endswith(".jsonl"):
            continue
        if purpose not in NOT_BATCH_INPUT:
            continue
        if str(f.get("id")) in (used_ids or set()):
            continue
        out.append({"id": str(f.get("id")), "filename": name,
                    "purpose": purpose, "bytes": int(f.get("bytes") or 0)})
    return sorted(out, key=lambda r: r["id"])


def within_window(batch, now, days):
    """True when the batch was created inside the window. Pure. days<=0 is all."""
    if not days or days <= 0:
        return True
    try:
        created = int((batch or {}).get("created_at") or 0)
    except (TypeError, ValueError):
        return False
    return created >= now - days * 86400


def verdict(failed, orphans, days):
    """Grade the run. Pure. Returns (state, detail)."""
    failed = list(failed or [])
    orphans = list(orphans or [])
    window = ("in the last %d days" % days) if days and days > 0 else "in the account"
    if failed and orphans:
        return ("validation-failed",
                "%d batch(es) failed input validation %s, and %d .jsonl was "
                "uploaded under a purpose /v1/batches will not accept"
                % (len(failed), window, len(orphans)))
    if failed:
        return ("validation-failed",
                "%d batch(es) failed input validation %s and nothing polled "
                "them to find out" % (len(failed), window))
    if orphans:
        return ("orphan-input-files",
                "%d .jsonl file(s) sit under a purpose /v1/batches will not "
                "accept, referenced by no batch" % len(orphans))
    return ("validation-clean",
            "no batch %s failed validation, and every .jsonl in the file list "
            "either carries purpose=batch or was used by a batch" % window)


def repair_lines(state, codes):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state == "validation-clean":
        return ["nothing to change. Keep the assertion that a submitter only "
                "logs success once status has left \"validating\"."]
    lines = []
    for code in sorted(set(codes or [])):
        if code in KNOWN_CODES:
            lines.append("%s: %s" % (code, KNOWN_CODES[code]))
    if state == "validation-failed":
        lines.append("fix the input at the reported lines, then re-upload with "
                     "purpose=batch and create the batch again. Nothing was "
                     "billed, so nothing needs reconciling.")
        lines.append("make the submitter poll. A 200 from batch creation is a "
                     "receipt, not a result: the only honest success signal is "
                     "a batch that has left \"validating\".")
    if state == "orphan-input-files":
        lines.append("re-upload each file with purpose=batch and delete the "
                     "mis-purposed copy, which counts against project storage "
                     "until you do.")
        lines.append("assert in the upload helper that the purpose matches the "
                     "endpoint that will consume the file.")
    return lines


def get_json(url, key, params=None, timeout=30):
    """One GET. Returns (payload, error). Read only, always."""
    try:
        r = requests.get(url, headers={"Authorization": "Bearer %s" % key},
                         params=params or {}, timeout=timeout)
    except requests.RequestException as exc:
        return (None, "request failed: %s" % exc)
    if r.status_code != 200:
        detail = ""
        try:
            detail = str((r.json().get("error") or {}).get("message") or "")
        except ValueError:
            detail = (r.text or "")[:160]
        return (None, "HTTP %d %s" % (r.status_code, detail))
    try:
        return (r.json(), None)
    except ValueError:
        return (None, "response was not JSON")


def page_all(url, key, params, max_pages):
    """Follow the after cursor. Returns (rows, error). GETs only."""
    rows = []
    after = None
    for _ in range(max(1, max_pages)):
        query = dict(params or {})
        if after:
            query["after"] = after
        payload, err = get_json(url, key, query)
        if err:
            return (rows, err)
        data = payload.get("data") or []
        rows.extend(data)
        if not payload.get("has_more") or not data:
            break
        after = data[-1].get("id")
        if not after:
            break
    return (rows, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since-days", type=int, default=30,
                    help="only report batches created inside this window (0 = all)")
    ap.add_argument("--max-pages", type=int, default=20,
                    help="cap on pages of 100 for each list; a bounded read "
                         "gives a bounded answer and the output says so")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key set to Read Only")
        return 2

    now = int(time.time())
    batches, err = page_all(BATCHES_URL, key, {"limit": PAGE}, args.max_pages)
    if err and not batches:
        log.error("could not read the batch list: %s", err)
        return 2
    if err:
        log.warning("batch list stopped early: %s", err)

    scoped = [b for b in batches if within_window(b, now, args.since_days)]
    failed = failed_batches(scoped)
    seen_codes = []
    for b in failed:
        billed = "nothing billed (0 requests)" if nothing_billed(b) \
            else "request_counts is not all zero, which is unusual for failed"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(int(b.get("failed_at")
                                              or b.get("created_at") or 0)))
        log.warning("%-16s failed at %s  %s", b.get("id"), stamp, billed)
        groups = lines_by_code(error_rows(b))
        if not groups:
            log.warning("  (the errors object is empty, so the reason is not "
                        "readable from the API)")
        for code, (lines, count, message, param) in groups.items():
            seen_codes.append(code)
            shown = ", ".join(str(n) for n in lines[:6])
            more = " and %d more" % (len(lines) - 6) if len(lines) > 6 else ""
            where = ("lines %s%s" % (shown, more)) if lines else "no line given"
            extra = "  param %s" % param if param else ""
            log.warning("  %-26s %s%s", code, where, extra)
            if message:
                log.info("  %-26s %s", "", message[:140])

    files, ferr = page_all(FILES_URL, key, {"limit": 10000}, args.max_pages)
    if ferr:
        log.warning("file list stopped early: %s", ferr)
    orphans = mispurposed_inputs(files, batch_input_ids(batches))
    for row in orphans:
        log.warning("orphan-input    %s  %s  purpose=%s  %.1f MB", row["id"],
                    row["filename"], row["purpose"], row["bytes"] / 1048576.0)

    state, detail = verdict(failed, orphans, args.since_days)
    emit = log.warning if state in FINDINGS else log.info
    emit("%-20s %s", state, detail)
    emit("  measured: status, errors.data[] and request_counts from the batch "
         "list, purpose from the file list")
    emit("  inferred: that the pipeline never polled, since a failed batch is "
         "otherwise indistinguishable from one nobody re-ran on purpose")
    for line in repair_lines(state, seen_codes):
        emit("  repair: %s", line)

    total = len(failed) + len(orphans)
    log.info("%d finding(s)", total)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
