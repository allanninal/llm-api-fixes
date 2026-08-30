"""Size the Anthropic requests that were attempted and never served.

Read only. One GET against the Admin API, which needs an Admin API key
(sk-ant-admin...); a workspace key is rejected by every /v1/organizations/*
path, and an Admin key can be provisioned read-only.

The messages usage report carries token sums and no request count at all, so
"requests the platform served" cannot be read. It is estimated from the work
that was done: median tokens per attempt as a baseline, billed tokens divided
by it, subtracted from your own attempt counter. Every number here is an
estimate and the output says so.

Nothing is retried and nothing is sent. A script that starts re-issuing traffic
into a platform that is over capacity is worse than the bug it found.
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_overload_residual")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# Every token field the messages usage report returns. cache_creation is a
# nested object; a parser looking for a flat cache_creation_input_tokens sums
# zero and reports a heavily cached minute as one where nothing happened.
TOKEN_FIELDS = ("uncached_input_tokens", "input_tokens",
                "cache_read_input_tokens", "output_tokens")
CACHE_CREATION_FIELDS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")

FINDINGS = ("overload-cluster",)


def _int(value):
    """Read a usage field as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def minute_key(stamp):
    """Normalise a timestamp to a UTC minute key. Pure. None if unreadable.

    Accepts the RFC 3339 strings the usage report returns and the shapes your
    own counter is likely to emit: with or without seconds, with a space
    instead of a T, or as epoch seconds. Two sources that disagree about
    timestamp format produce a comparison with no overlap and a clean bill of
    health, which is the worst possible failure for this check.
    """
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        try:
            when = dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
        return when.strftime("%Y-%m-%dT%H:%MZ")
    text = str(stamp or "").strip().replace(" ", "T")
    if len(text) < 16:
        return None
    head = text[:16]
    if head[4] != "-" or head[7] != "-" or head[10] != "T" or head[13] != ":":
        return None
    for part in (head[0:4], head[5:7], head[8:10], head[11:13], head[14:16]):
        if not part.isdigit():
            return None
    return head + "Z"


def minute_index(key):
    """Minutes since the epoch for a minute key. Pure. None if unreadable.

    Adjacency is the whole finding, so it needs to be arithmetic on integers
    rather than string comparison, which gets 14:59 and 15:00 wrong.
    """
    normalised = minute_key(key)
    if normalised is None:
        return None
    try:
        when = dt.datetime(int(normalised[0:4]), int(normalised[5:7]),
                           int(normalised[8:10]), int(normalised[11:13]),
                           int(normalised[14:16]), tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return int(when.timestamp()) // 60


def tokens_by_minute(buckets):
    """Total billed tokens per minute. Pure.

    Every field is summed, cache reads and cache creation included, because the
    question is whether the platform did any work in that minute and not what
    the work cost.
    """
    out = {}
    for bucket in buckets or []:
        key = minute_key(bucket.get("starting_at") or bucket.get("start_time"))
        if key is None:
            continue
        total = 0
        for result in bucket.get("results") or []:
            for field in TOKEN_FIELDS:
                total += _int(result.get(field))
            creation = result.get("cache_creation") or {}
            for field in CACHE_CREATION_FIELDS:
                total += _int(creation.get(field))
        out[key] = out.get(key, 0) + total
    return out


def attempts_by_minute(raw):
    """Read your own attempt counter into minute keys. Pure.

    Accepts {"2026-08-30T14:03Z": 900} or {"...": {"attempts": 900}}. Minutes
    that cannot be parsed are dropped rather than folded into a neighbour: an
    attempt attributed to the wrong minute breaks the contiguity test, which is
    the only thing separating a finding from noise.
    """
    out = {}
    for stamp, value in (raw or {}).items():
        key = minute_key(stamp)
        if key is None:
            continue
        if isinstance(value, dict):
            count = _int(value.get("attempts"))
        elif isinstance(value, bool):
            count = 0
        else:
            count = _int(value)
        out[key] = out.get(key, 0) + count
    return out


def _median(values):
    """Median of a list of numbers. Pure. None when empty."""
    ordered = sorted(values or [])
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2.0


def baseline_tokens_per_attempt(tokens, attempts, min_minutes=5, min_attempts=1):
    """Median tokens per attempt across the covered minutes. Pure.

    The median, never the mean. The minutes this script is hunting are exactly
    the ones that would drag a mean down, so a mean baseline absorbs the loss
    it was computed to reveal and the check comes back clean during an outage.
    """
    ratios = []
    for key, made in (attempts or {}).items():
        made = _int(made)
        if made < min_attempts:
            continue
        ratios.append(_int((tokens or {}).get(key)) / float(made))
    if len(ratios) < min_minutes:
        return None
    value = _median(ratios)
    return value if value and value > 0 else None


def residual_rows(tokens, attempts, baseline):
    """One row per minute: attempts, tokens, estimated served and residual. Pure.

    served is tokens / baseline, which is an estimate and the only one
    available: the report has no request count to read instead.
    """
    out = []
    if not baseline or baseline <= 0:
        return out
    for key in sorted(attempts or {}):
        made = _int(attempts.get(key))
        if made <= 0:
            continue
        billed = _int((tokens or {}).get(key))
        served = billed / float(baseline)
        residual = max(0.0, made - served)
        out.append({"minute": key, "index": minute_index(key), "attempts": made,
                    "tokens": billed, "served": served, "residual": residual,
                    "share": residual / float(made)})
    return out


def clusters(rows, floor=0.3, min_attempts=20):
    """Group the shortfall minutes into contiguous runs. Pure.

    Contiguity is the finding. A request that starts at 14:03:58 and finishes
    at 14:04:06 lands its attempt in one minute and its tokens in the next, so
    isolated minutes are bucket arithmetic. A platform capacity condition is
    not a coin flip per request and arrives as a run.
    """
    bad = [r for r in rows or []
           if r.get("index") is not None
           and _int(r.get("attempts")) >= min_attempts
           and float(r.get("share") or 0.0) >= floor]
    bad.sort(key=lambda r: r["index"])

    runs = []
    for row in bad:
        if runs and row["index"] == runs[-1][-1]["index"] + 1:
            runs[-1].append(row)
        else:
            runs.append([row])
    return runs


def classify(cluster, min_minutes=3):
    """Classify one run of minutes. Pure. Returns (state, detail)."""
    cluster = cluster or []
    if not cluster:
        return ("no-cluster", "nothing to classify")
    attempts = sum(_int(r.get("attempts")) for r in cluster)
    lost = sum(float(r.get("residual") or 0.0) for r in cluster)
    share = (lost / attempts) if attempts else 0.0
    detail = ("%s through %s: %d attempt(s) over %d minute(s), about %d of them "
              "produced no billed tokens (%.0f%%)"
              % (cluster[0]["minute"], cluster[-1]["minute"], attempts,
                 len(cluster), int(lost), share * 100))
    if len(cluster) < min_minutes:
        return ("single-minute-dip",
                detail + ". Shorter than the %d minute floor, so this is most "
                "likely a request that straddled a bucket boundary rather than "
                "a capacity condition." % min_minutes)
    return ("overload-cluster",
            detail + ". A run this long is a platform capacity condition, "
            "which is what 529 is, and it is retryable.")


def excess_minutes(rows, tolerance=0.25):
    """Minutes where far more work was billed than the attempts explain. Pure.

    The opposite sign, and a different note. Being billed for tokens your own
    counter cannot account for is a recording gap in your telemetry, not
    requests the platform failed to serve.
    """
    out = []
    for row in rows or []:
        made = _int(row.get("attempts"))
        if made <= 0:
            continue
        if float(row.get("served") or 0.0) > made * (1.0 + tolerance):
            out.append(row["minute"])
    return out


def tiers_seen(buckets):
    """Every service_tier value present in the window. Pure.

    Priority Tier used to be the answer to 529 and capacity commitments are no
    longer sold, so "none of your traffic was served as priority" is usually
    the true and useful thing to print.
    """
    out = set()
    for bucket in buckets or []:
        for result in bucket.get("results") or []:
            tier = str(result.get("service_tier") or "").strip()
            if tier:
                out.add(tier)
    return out


def window_start(minutes):
    """Floor to the minute: starting_at has to sit on a bucket boundary."""
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    return (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def read_buckets(session, path, params):
    """Walk the paginated usage report."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--attempts", required=True,
                    help="JSON file of the requests your client attempted, "
                         "keyed by minute")
    ap.add_argument("--minutes", type=int, default=240,
                    help="minutes of one-minute buckets to read (max 1440)")
    ap.add_argument("--floor", type=float, default=0.3,
                    help="residual share above which a minute joins a cluster "
                         "(default 0.3)")
    ap.add_argument("--min-cluster", type=int, default=3,
                    help="adjacent minutes needed to call it a cluster "
                         "(default 3)")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key "
                  "(sk-ant-admin...); a workspace key cannot read "
                  "/v1/organizations/*")
        return 2

    try:
        with open(args.attempts, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("could not read %s: %s", args.attempts, exc)
        return 2
    if not isinstance(raw, dict):
        log.error("%s should be a JSON object keyed by minute", args.attempts)
        return 2

    minutes = max(1, min(int(args.minutes), 1440))
    session = requests.Session()
    session.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    buckets = list(read_buckets(session, "/organizations/usage_report/messages", {
        "starting_at": window_start(minutes),
        "bucket_width": "1m",
        "limit": minutes,
        "group_by[]": ["service_tier"],
    }))

    tokens = tokens_by_minute(buckets)
    attempts = attempts_by_minute(raw)
    if not attempts:
        log.error("no readable minutes in %s. Keys should look like "
                  "2026-08-30T14:03Z", args.attempts)
        return 2

    baseline = baseline_tokens_per_attempt(tokens, attempts)
    if baseline is None:
        log.info("not enough overlapping minutes to establish a baseline; "
                 "nothing can be said about loss in this window")
        return 0
    log.info("baseline %d token(s) per attempt, taken as the median across "
             "%d minute(s)", int(baseline), len(attempts))

    rows = residual_rows(tokens, attempts, baseline)
    found = 0
    for cluster in clusters(rows, args.floor):
        state, detail = classify(cluster, args.min_cluster)
        if state in FINDINGS:
            found += 1
            log.warning("%-18s %s", state, detail)
        else:
            log.info("%-18s %s", state, detail)

    over = excess_minutes(rows)
    if over:
        log.warning("  %d minute(s) billed far more work than your attempts "
                    "explain, starting at %s. That is the opposite sign and a "
                    "different problem: tokens you were billed for and did not "
                    "record.", len(over), over[0])

    tiers = tiers_seen(buckets)
    if tiers and "priority" not in tiers:
        log.info("  no traffic in this window was served as priority (%s)",
                 ", ".join(sorted(tiers)))

    if found:
        log.warning("  repair: put 429, every 5xx and 529 in one retryable "
                    "class with exponential backoff and jitter, or use the "
                    "SDK's own retry instead of a hand-rolled except. 529 is "
                    "overloaded_error and is a platform capacity condition, "
                    "not something your request caused.")
        log.warning("  repair: capture the request-id header from every "
                    "response including errors. It is the only identifier "
                    "support can act on, and this report cannot recover it "
                    "after the fact.")

    log.info("%d minute(s) compared, %d cluster(s)", len(rows), found)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
