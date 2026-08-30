"""Find Anthropic keys whose cache is rewritten on every call and never read.

Read only. One GET against the Admin API, which needs an Admin API key
(sk-ant-admin...); a workspace key is rejected by every /v1/organizations/
path, and an Admin key can be provisioned read-only.

Totals cannot tell this apart from two neighbouring problems, so the evidence
is spacing. A run of adjacent one-minute buckets that each write a cache entry
and never read one is longer than the entry's own TTL, which means the entry
was alive and unmatched the whole time. Only a prefix that differs on every
call does that. Caching switched off, and caching that is read but not read
enough, are named and handed to their own notes.

The repair is printed, never performed. Moving a timestamp is a deploy.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_cache_prefix_churn")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# cache_creation is a nested object. A parser looking for a flat
# cache_creation_input_tokens sums zero and reports a key that writes on every
# call as one that never caches at all, which is the opposite finding.
CACHE_CREATION_FIELDS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")

FINDINGS = ("prefix-churn",)


def _int(value):
    """Read a usage field as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def minute_key(stamp):
    """Normalise a timestamp to a UTC minute key. Pure. None if unreadable."""
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


def minute_index(stamp):
    """Minutes since the epoch. Pure. None if unreadable.

    Adjacency is the entire finding, so it has to be arithmetic on integers.
    String comparison puts 14:59 and 15:00 two apart, which breaks every run
    that crosses an hour boundary and quietly halves the longest one.
    """
    key = minute_key(stamp)
    if key is None:
        return None
    try:
        when = dt.datetime(int(key[0:4]), int(key[5:7]), int(key[8:10]),
                           int(key[11:13]), int(key[14:16]), tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return int(when.timestamp()) // 60


def rows_by_key(buckets):
    """Per (api_key_id, model), one row per minute, sorted. Pure."""
    merged = {}
    for bucket in buckets or []:
        stamp = bucket.get("starting_at") or bucket.get("start_time")
        key = minute_key(stamp)
        index = minute_index(stamp)
        if key is None or index is None:
            continue
        for result in bucket.get("results") or []:
            if not isinstance(result, dict):
                continue
            ident = (str(result.get("api_key_id") or "unknown"),
                     str(result.get("model") or "unknown"))
            creation = result.get("cache_creation") or {}
            row = merged.setdefault((ident, index),
                                    {"minute": key, "index": index, "uncached": 0,
                                     "write5m": 0, "write1h": 0, "reads": 0})
            row["uncached"] += _int(result.get("uncached_input_tokens"))
            row["write5m"] += _int(creation.get("ephemeral_5m_input_tokens"))
            row["write1h"] += _int(creation.get("ephemeral_1h_input_tokens"))
            row["reads"] += _int(result.get("cache_read_input_tokens"))
    out = {}
    for (ident, _index), row in merged.items():
        out.setdefault(ident, []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: r["index"])
    return out


def writes(row):
    """Cache creation tokens in one minute, both TTLs. Pure."""
    return _int((row or {}).get("write5m")) + _int((row or {}).get("write1h"))


def write_share(row):
    """Share of a minute's input that was written as a fresh cache entry. Pure.

    None when nothing was sent, which is a different state from zero: an idle
    minute must not be counted as a minute that cached nothing.
    """
    total = _int((row or {}).get("uncached")) + writes(row)
    if total <= 0:
        return None
    return writes(row) / float(total)


def totals(rows):
    """Sum a series, and count the minutes that carried any traffic. Pure."""
    out = {"uncached": 0, "write5m": 0, "write1h": 0, "reads": 0, "active": 0}
    for row in rows or []:
        out["uncached"] += _int(row.get("uncached"))
        out["write5m"] += _int(row.get("write5m"))
        out["write1h"] += _int(row.get("write1h"))
        out["reads"] += _int(row.get("reads"))
        if _int(row.get("uncached")) + writes(row) + _int(row.get("reads")) > 0:
            out["active"] += 1
    out["writes"] = out["write5m"] + out["write1h"]
    return out


def churn_runs(rows, share_floor=0.5, read_floor=0.01):
    """Maximal runs of adjacent minutes that wrote and never read. Pure.

    This is the finding and nothing else in the section computes it. A five
    minute entry written in the first minute of a run is still alive in the
    fifth, so a run that long with no read in it means the entry was live and
    unmatched throughout. Neither a cold start nor a TTL expiring between calls
    can produce that; a prefix that differs on every call is the only thing
    that can.
    """
    runs = []
    current = []
    for row in rows or []:
        made = writes(row)
        share = write_share(row)
        churning = (made > 0 and share is not None and share >= share_floor
                    and _int(row.get("reads")) <= made * read_floor)
        if not churning:
            if current:
                runs.append(current)
                current = []
            continue
        if current and _int(row.get("index")) == _int(current[-1].get("index")) + 1:
            current.append(row)
        else:
            if current:
                runs.append(current)
            current = [row]
    if current:
        runs.append(current)
    return runs


def gap_profile(rows):
    """Median gap in minutes between minutes that wrote. Pure. None under two.

    The alternative explanation, stated as a number. Traffic arriving less
    often than the TTL writes an entry that expires before anything can read
    it, and that is a different note with a different repair. Its signature is
    isolated writing minutes; churn's is adjacent ones.
    """
    indices = [_int(r.get("index")) for r in rows or [] if writes(r) > 0]
    indices.sort()
    if len(indices) < 2:
        return None
    gaps = sorted(indices[i + 1] - indices[i] for i in range(len(indices) - 1))
    middle = len(gaps) // 2
    if len(gaps) % 2:
        return float(gaps[middle])
    return (gaps[middle - 1] + gaps[middle]) / 2.0


def ttl_split(sums):
    """Which TTL the writes were bought at. Pure. Returns (state, detail).

    It changes how damning a run is and what it cost. A 5 minute entry has to
    be matched within five minutes and is billed at 1.25x base input; a 1 hour
    entry is alive for sixty and is billed at 2x, so an adjacent run against
    hour-long writes is both stronger evidence and twice the surcharge.
    """
    sums = sums or {}
    five = _int(sums.get("write5m"))
    hour = _int(sums.get("write1h"))
    if five + hour <= 0:
        return ("no-writes", "nothing was written to the cache in this window")
    if hour > five:
        return ("1h-dominant",
                "the writes are mostly 1 hour entries at 2x base input, so each "
                "one was alive for sixty minutes and never matched in any of them")
    if five > hour:
        return ("5m-dominant",
                "the writes are 5 minute entries at 1.25x base input, so any run "
                "longer than five minutes outlived calls that never matched it")
    return ("mixed", "the writes are split evenly between the 5 minute and 1 "
                     "hour TTLs")


def handoff(state):
    """Which note owns this shape, when it is not this one. Pure.

    Three findings read the same two numbers. Naming the other two in the
    output is the difference between a check that classifies and a check that
    claims everything it sees.
    """
    if state == "caching-off":
        return ("no writes and no reads anywhere: caching was never switched "
                "on for this key. Read the prompt-caching-never-used note; the "
                "loss there is a discount not taken rather than a surcharge "
                "paid.")
    if state == "cache-is-read":
        return ("entries are being matched, so the prefix is stable enough to "
                "hit. Whether it hits often enough to pay for the write "
                "premium is the write-to-read ratio, which is the "
                "cache-writes-with-no-reads note.")
    if state == "gap-driven-misses":
        return ("the writing minutes are isolated rather than adjacent, so each "
                "entry plausibly expired before the next call arrived. That is "
                "arrival rate against TTL, and it is the "
                "cache-writes-with-no-reads note rather than this one.")
    return ""


def classify(rows, min_run=5, share_floor=0.5, read_floor=0.01, min_active=10):
    """Classify one key and model series. Pure. Returns (state, detail).

    The first three branches exist to give the finding away. Only a series with
    writes, no reads, a majority write share and adjacent writing minutes
    belongs to this note.
    """
    sums = totals(rows)
    if sums["active"] < min_active:
        return ("too-little-traffic",
                "%d active minute(s), under the floor of %d. Nothing can be "
                "said about spacing with fewer." % (sums["active"], min_active))

    if sums["writes"] == 0 and sums["reads"] == 0:
        return ("caching-off",
                "%d uncached input token(s), no cache writes and no cache reads"
                % sums["uncached"])
    if sums["writes"] == 0:
        return ("reads-only",
                "%d cache read(s) and no writes in this window: the entries "
                "were written before it started" % sums["reads"])
    if sums["reads"] > sums["writes"] * read_floor:
        return ("cache-is-read",
                "%d cache read token(s) against %d written"
                % (sums["reads"], sums["writes"]))

    share = sums["writes"] / float(sums["uncached"] + sums["writes"])
    if share < share_floor:
        return ("small-cached-prefix",
                "writes are %.0f%% of input with reads at 0, under the floor of "
                "%.0f%%. Something is being cached and never matched, and it is "
                "a minority of the prompt rather than the prefix."
                % (share * 100, share_floor * 100))

    runs = churn_runs(rows, share_floor, read_floor)
    longest = max(runs, key=len) if runs else []
    if len(longest) >= min_run:
        return ("prefix-churn",
                "writes are %.0f%% of input with reads at 0; longest run %d "
                "adjacent minute(s) from %s to %s. The entry written at the "
                "start of that run was still alive at the end and was never "
                "matched, so the prefix differs on every call."
                % (share * 100, len(longest), longest[0]["minute"],
                   longest[-1]["minute"]))

    gap = gap_profile(rows)
    if gap is not None and gap > min_run:
        return ("gap-driven-misses",
                "writes are %.0f%% of input with reads at 0, and the writing "
                "minutes sit a median of %.0f minute(s) apart"
                % (share * 100, gap))

    return ("intermittent-misses",
            "writes are %.0f%% of input with reads at 0, and the longest run of "
            "adjacent writing minutes is %d, under the floor of %d. Suggestive "
            "and not conclusive: widen the window."
            % (share * 100, len(longest), min_run))


def repair_lines():
    """The invalidator hunt, in cache order. Pure."""
    return [
        "hunt the invalidator in cache order: tools, then system, then "
        "messages. A change to the tools invalidates all three.",
        "the usual suspects are a clock (datetime.now in a system prompt), a "
        "tool list built from an unordered dict, a per-request id, a per-user "
        "preamble placed before the breakpoint, and an option toggled per call "
        "such as tool_choice, citations, web search or reasoning effort.",
        "move each one strictly after the last cache_control breakpoint, then "
        "re-read these same minute buckets. The runs should break up before "
        "the totals move.",
    ]


def window_start(minutes):
    """Floor to the minute: starting_at has to sit on a bucket boundary."""
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    return (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/ needs an Admin "
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
    ap.add_argument("--minutes", type=int, default=240,
                    help="minutes of one-minute buckets to read (max 1440)")
    ap.add_argument("--min-run", type=int, default=5,
                    help="adjacent writing minutes with no read that make a "
                         "finding (default 5, the 5m TTL)")
    ap.add_argument("--share-floor", type=float, default=0.5,
                    help="write share of input above which the prefix, rather "
                         "than a fragment of it, is being rewritten")
    ap.add_argument("--show-all", action="store_true",
                    help="also print series that are behaving")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key "
                  "(sk-ant-admin...); a workspace key cannot read "
                  "/v1/organizations/")
        return 2

    minutes = max(30, min(int(args.minutes), 1440))
    session = requests.Session()
    session.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    buckets = read_buckets(session, "/organizations/usage_report/messages", {
        "starting_at": window_start(minutes),
        "bucket_width": "1m",
        "limit": minutes,
        "group_by[]": ["api_key_id", "model"],
    })

    series = rows_by_key(buckets)
    if not series:
        log.info("no messages usage in the last %d minute(s)", minutes)
        return 0

    checked = 0
    bad = 0
    for ident in sorted(series):
        rows = series[ident]
        state, detail = classify(rows, args.min_run, args.share_floor)
        checked += 1
        line = "%-20s %s / %s  %s" % (state, ident[0], ident[1], detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            _, ttl = ttl_split(totals(rows))
            log.warning("  %s", ttl)
            log.warning("  note: grouped by key and model. A key serving many "
                        "tenants with a per tenant prefix writes constantly and "
                        "correctly; this finding is strongest on a key with one "
                        "workload.")
            for repair in repair_lines():
                log.warning("  repair: %s", repair)
        else:
            note = handoff(state)
            if note:
                log.info(line)
                log.info("  %s", note)
            elif args.show_all or state == "intermittent-misses":
                log.info(line)

    log.info("%d key/model series checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
