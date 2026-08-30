"""Sum every page of the file store and grade it against a documented ceiling.

Read only. GET /v1/files and nothing else, on either provider. Nothing is
uploaded, nothing is deleted, and no file content is ever fetched: every
finding here is made of sizes, purposes, dates and ids.

Neither provider exposes the quota. There is no endpoint that returns a limit,
a consumed figure or a remaining one, so the ceiling below is a documented
constant that no request can confirm while the total is measured by summing a
field over every page. Those are different kinds of fact and the output keeps
them in different columns.

The ceilings also sit on different containers. OpenAI documents 2.5 TB per
project and no organization-wide limit at all; Anthropic documents 1 TB per
organization while its Files API is workspace scoped. So a single run measures
one project or one workspace, and says which.
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
log = logging.getLogger("file_store_quota_audit")

ENDPOINTS = {"openai": "https://api.openai.com/v1/files",
             "anthropic": "https://api.anthropic.com/v1/files"}

# Documented ceilings, not readable ones. Overridable with --quota-bytes,
# because a negotiated limit and a republished docs page look identical here.
DOC_QUOTA_BYTES = {"openai": 2_500_000_000_000, "anthropic": 1_000_000_000_000}
DOC_QUOTA_LABEL = {"openai": "2.5 TB per project",
                   "anthropic": "1 TB per organization"}
DOC_FILE_CAP_BYTES = {"openai": 512_000_000, "anthropic": 500_000_000}
SIZE_FIELD = {"openai": "bytes", "anthropic": "size_bytes"}
KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}

FINDINGS = ("quota-critical", "quota-warning", "purpose-dominates",
            "file-near-cap", "no-expiry-policy")

_RFC3339 = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})"
                      r"(?:\.\d+)?(Z|z|[+-]\d{2}:?\d{2})?$")


def epoch(value):
    """Seconds since the epoch from either provider's shape. Pure.

    OpenAI returns integer Unix seconds. Anthropic returns RFC 3339 strings.
    Returns 0 for anything unparseable, and 0 means unknown everywhere it is
    read rather than meaning 1970.
    """
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


def file_row(body, provider):
    """One file object, normalised. Pure. Two providers, one shape.

    OpenAI calls the size `bytes` and carries a `purpose`; Anthropic calls it
    `size_bytes` and has no purpose concept at all, so this refuses to invent
    one. `expires_at` is optional rather than nullable on OpenAI, meaning the
    key is simply absent on a file with no expiry, and on Anthropic it is
    absent whenever the files-api-2025-04-14 beta header was sent.
    """
    body = body if isinstance(body, dict) else {}
    try:
        size = int(body.get(SIZE_FIELD.get(provider, "bytes")))
    except (TypeError, ValueError):
        size = 0
    return {"id": str(body.get("id") or ""),
            "filename": str(body.get("filename") or ""),
            "size": max(0, size),
            "purpose": str(body.get("purpose") or "unclassified"),
            "created_at": epoch(body.get("created_at")),
            "expires_at": epoch(body.get("expires_at")) or None,
            "expiry_reported": "expires_at" in body}


def human(size):
    """Binary units, one decimal. Pure. Used everywhere rather than inlined."""
    try:
        n = float(size)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return "%d B" % int(n) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TiB" % n


def totals(rows):
    """Count and summed bytes. Pure."""
    rows = rows or []
    return {"count": len(rows), "bytes": sum(int(r.get("size") or 0) for r in rows)}


def by_purpose(rows):
    """Per-purpose count and bytes, largest first. Pure."""
    acc = {}
    for row in rows or []:
        key = str(row.get("purpose") or "unclassified")
        cur = acc.setdefault(key, {"count": 0, "bytes": 0})
        cur["count"] += 1
        cur["bytes"] += int(row.get("size") or 0)
    return sorted(([k, v["count"], v["bytes"]] for k, v in acc.items()),
                  key=lambda item: (-item[2], item[0]))


def grade_total(total_bytes, quota_bytes, warn_share=0.60, critical_share=0.85):
    """Share of a documented ceiling. Pure. The ceiling is an argument.

    An argument rather than a constant because the number came out of a docs
    page: it can be renegotiated for one account and republished for everyone,
    and neither event is visible from any GET.
    """
    try:
        quota, used = int(quota_bytes), int(total_bytes)
    except (TypeError, ValueError):
        quota, used = 0, 0
    if quota <= 0:
        return ("quota-unknown", "no usable ceiling was supplied, so the total "
                                 "is a number without a denominator")
    share = used / float(quota)
    detail = ("%.1f%% of the documented ceiling is in use, with about %s of "
              "headroom before uploads start to fail"
              % (share * 100, human(max(0, quota - used))))
    if share >= critical_share:
        return ("quota-critical", detail)
    if share >= warn_share:
        return ("quota-warning", detail)
    return ("quota-headroom",
            "%.1f%% of the documented ceiling is in use, %s of headroom"
            % (share * 100, human(max(0, quota - used))))


def grade_concentration(purposes, total_bytes, share=0.40):
    """The purpose class worth sweeping first. Pure."""
    try:
        total = int(total_bytes)
    except (TypeError, ValueError):
        total = 0
    if not purposes or total <= 0:
        return ("purpose-even", "nothing to concentrate: the store is empty or "
                                "carries no size information")
    name, count, size = purposes[0]
    got = size / float(total)
    if got < share:
        return ("purpose-even",
                "no single purpose holds more than %.0f%% of the store; the "
                "largest is %s at %.1f%%" % (share * 100, name, got * 100))
    return ("purpose-dominates",
            "%s is %.1f%% of the store, %d file(s)" % (name, got * 100, count))


def grade_outliers(rows, cap_bytes, warn_share=0.80):
    """The second ceiling, per file and not a fraction of the first. Pure."""
    try:
        cap = int(cap_bytes)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return ("cap-unknown", "no per-file cap was supplied", [])
    floor = cap * warn_share
    big = sorted((r for r in rows or [] if int(r.get("size") or 0) >= floor),
                 key=lambda r: -int(r.get("size") or 0))
    if not big:
        return ("file-sizes-fine",
                "no file is within %.0f%% of the per-file cap"
                % (warn_share * 100), [])
    return ("file-near-cap",
            "%d file(s) above %.0f%% of the per-file cap"
            % (len(big), warn_share * 100), big)


def grade_expiry(rows, now, stale_days):
    """The only reading here that describes the future. Pure."""
    rows = rows or []
    if not rows:
        return ("expiry-none", "the store is empty")
    unexpiring = [r for r in rows if not r.get("expires_at")]
    if not unexpiring:
        return ("expiry-covered",
                "every file carries an expires_at, so this store has a "
                "lifecycle rather than a trajectory")
    cutoff = int(now) - int(stale_days) * 86400
    stale = [r for r in unexpiring
             if r.get("created_at") and int(r["created_at"]) < cutoff]
    return ("no-expiry-policy",
            "%d of %d file(s) have no expires_at, and %d of those are older "
            "than %d day(s)"
            % (len(unexpiring), len(rows), len(stale), int(stale_days)))


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state in ("quota-critical", "quota-warning"):
        return ["sweep the purpose class named below, then set an expiry at "
                "upload so the next two thirds take longer to arrive than the "
                "last did."]
    if state == "purpose-dominates":
        return ["delete the ones whose job is finished and read, one at a "
                "time, with DELETE /v1/files/{file_id}. Nothing here does that "
                "for you, a deleted file cannot be recovered, and on OpenAI "
                "the deletion also removes the file from every vector store "
                "holding it."]
    if state == "file-near-cap":
        return ["a second ceiling, unrelated to the total. Split these at "
                "source rather than making room for them."]
    if state == "no-expiry-policy":
        return ["upload with an expiry so this population stops being "
                "unbounded: expires_after with an anchor of created_at on "
                "OpenAI (3600 to 2592000 seconds), expires_in_seconds on "
                "Anthropic (3600 to 7776000).",
                "for what is already there, confirm by hand and then delete. "
                "Nothing in the metadata can tell an audit which files matter."]
    if state == "quota-unknown":
        return ["pass --quota-bytes. Without a denominator this run is an "
                "inventory rather than an audit."]
    return []


def fetch_openai(key, max_pages, timeout=30):
    """Page GET /v1/files on `after`. Returns (rows, pages, complete)."""
    rows, cursor, pages = [], None, 0
    while pages < max_pages:
        params = {"limit": 10000, "order": "asc"}
        if cursor:
            params["after"] = cursor
        try:
            r = requests.get(ENDPOINTS["openai"], params=params,
                             headers={"Authorization": "Bearer " + key},
                             timeout=timeout)
        except requests.RequestException as exc:
            log.error("openai listing failed: %s", exc)
            return (rows, pages, False)
        if r.status_code != 200:
            log.error("openai listing returned HTTP %s", r.status_code)
            return (rows, pages, False)
        body = r.json() if r.content else {}
        data = body.get("data") or []
        pages += 1
        rows.extend(file_row(item, "openai") for item in data)
        # has_more is a required field on this response, so it is authoritative
        # where it appears. The short-page fallback is only for a response that
        # does not honour its own schema, which is worth surviving rather than
        # trusting blindly.
        if body.get("has_more") is False or not data:
            return (rows, pages, True)
        if "has_more" not in body and len(data) < params["limit"]:
            return (rows, pages, True)
        cursor = data[-1].get("id")
        if not cursor:
            return (rows, pages, True)
    return (rows, pages, False)


def fetch_anthropic(key, max_pages, timeout=30):
    """Page GET /v1/files on `page`/`next_page`. Returns (rows, pages, complete).

    Sent without the files-api-2025-04-14 beta header on purpose: with it the
    response reverts to the older cursor shape and expires_at is not returned
    at all, which would silently remove a field this audit reads.
    """
    rows, page, pages = [], None, 0
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    while pages < max_pages:
        params = {"limit": 1000}
        if page:
            params["page"] = page
        try:
            r = requests.get(ENDPOINTS["anthropic"], params=params,
                             headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            log.error("anthropic listing failed: %s", exc)
            return (rows, pages, False)
        if r.status_code != 200:
            log.error("anthropic listing returned HTTP %s", r.status_code)
            return (rows, pages, False)
        body = r.json() if r.content else {}
        data = body.get("data") or []
        pages += 1
        rows.extend(file_row(item, "anthropic") for item in data)
        page = body.get("next_page")
        if not page:
            return (rows, pages, True)
    return (rows, pages, False)


def report(provider, rows, pages, complete, args, now):
    """Print one store's verdicts. Returns the number of findings."""
    quota = args.quota_bytes or DOC_QUOTA_BYTES[provider]
    cap = args.file_cap_bytes or DOC_FILE_CAP_BYTES[provider]
    tot = totals(rows)
    log.info("%-9s %d page(s) read, %d file(s), %s",
             provider, pages, tot["count"], human(tot["bytes"]))
    log.info("  measured: the sum of %s over every page of GET /v1/files",
             SIZE_FIELD[provider])
    log.info("  documented: a ceiling of %s, which no endpoint reports",
             DOC_QUOTA_LABEL[provider])
    if not complete:
        log.warning("  incomplete: paging stopped early, so %s is a floor and "
                    "not a total", human(tot["bytes"]))

    outlier_state, outlier_detail, big = grade_outliers(rows, cap)
    grades = [grade_total(tot["bytes"], quota),
              grade_concentration(by_purpose(rows), tot["bytes"]),
              (outlier_state, outlier_detail),
              grade_expiry(rows, now, args.stale_days)]

    findings = 0
    for state, detail in grades:
        emit = log.warning if state in FINDINGS else log.info
        emit("%-20s %s", state, detail)
        if state == "file-near-cap":
            for row in big[:5]:
                emit("%-20s %s  %s  %s", "", row["id"], human(row["size"]),
                     row["purpose"])
        for line in repair_lines(state):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", choices=("openai", "anthropic", "both"),
                    default="both", help="which file store to audit")
    ap.add_argument("--quota-bytes", type=int, default=0,
                    help="override the documented ceiling for your account")
    ap.add_argument("--file-cap-bytes", type=int, default=0,
                    help="override the documented per-file cap")
    ap.add_argument("--stale-days", type=int, default=90,
                    help="age at which a file with no expiry is worth listing")
    ap.add_argument("--max-pages", type=int, default=50,
                    help="stop after this many pages and report a floor")
    args = ap.parse_args()

    now = int(time.time())
    wanted = ("openai", "anthropic") if args.provider == "both" else (args.provider,)
    ran = findings = 0
    for provider in wanted:
        key = os.environ.get(KEY_ENV[provider])
        if not key:
            log.info("%-20s %s not set, so that store was not audited. An "
                     "unaudited store is not an empty one",
                     "not-audited", KEY_ENV[provider])
            continue
        fetch = fetch_openai if provider == "openai" else fetch_anthropic
        rows, pages, complete = fetch(key, args.max_pages)
        findings += report(provider, rows, pages, complete, args, now)
        ran += 1

    if not ran:
        log.error("set OPENAI_API_KEY (a project read key) or ANTHROPIC_API_KEY "
                  "(a key with access to the workspace). Every call is a GET of "
                  "/v1/files")
        return 2
    log.info("%d store(s) audited, %d finding(s)", ran, findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
