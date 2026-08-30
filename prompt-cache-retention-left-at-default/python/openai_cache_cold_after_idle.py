"""Find OpenAI traffic that runs cold in the hours that follow a gap.

Read only. One paginated GET against the Usage API, which needs an admin key
(sk-admin-...); a project key is rejected by every /v1/organization/ path, and
an admin key can be provisioned with the read scopes only.

Cached prefixes are evicted after an idle period, and the default window is
short. A nightly batch, a low-traffic tenant or a cron job that fires every few
hours therefore starts cold every single time, on a prefix that has not changed
in months. The signature is positional rather than arithmetic: the cold hours
are the ones that resume traffic after a gap, and the hours that follow a busy
hour are fine. Nothing about the prompt is wrong.

The finding is reported as the shortest gap length at which the share has
already collapsed, because that number and the retention setting are the same
number, and it is what tells you whether the repair is a parameter or a
schedule.

The repair is printed, never performed.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_cache_cold_after_idle")

API = "https://api.openai.com/v1"

# Gap lengths in hours, coarse enough that each bin maps onto a different
# repair. Ordered shortest first: the finding is the first one that is already
# cold, not the worst one.
BIN_ORDER = ("1h", "2-5h", "6-23h", "24h+")

FINDINGS = ("cold-after-idle",)


def _int(value):
    """Read a usage field as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def hour_index(stamp):
    """Hours since the epoch. Pure. None if unreadable.

    Gaps have to be integer arithmetic. Counting idle hours by comparing
    formatted stamps gets 23:00 and 00:00 wrong every night, and a nightly job
    is exactly the workload this note is about.
    """
    if isinstance(stamp, bool) or stamp is None:
        return None
    if isinstance(stamp, (int, float)):
        return int(stamp) // 3600
    text = str(stamp).strip().replace(" ", "T")
    if len(text) < 13:
        return None
    head = text[:13]
    if head[4] != "-" or head[7] != "-" or head[10] != "T":
        return None
    for part in (head[0:4], head[5:7], head[8:10], head[11:13]):
        if not part.isdigit():
            return None
    try:
        when = dt.datetime(int(head[0:4]), int(head[5:7]), int(head[8:10]),
                           int(head[11:13]), tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return int(when.timestamp()) // 3600


def hour_label(index):
    """Render an hour index back as a UTC stamp. Pure."""
    if index is None:
        return "unknown"
    when = dt.datetime.fromtimestamp(int(index) * 3600, dt.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:00Z")


def rows_by_series(buckets):
    """Per (project_id, model), one row per active hour, sorted. Pure.

    Only hours that actually carried traffic become rows. The idle hours are
    not rows and must not be: the gap is the distance between two rows, and a
    zero-filled series would have no gaps in it at all.
    """
    merged = {}
    for bucket in buckets or []:
        index = hour_index(bucket.get("start_time"))
        if index is None:
            continue
        for result in bucket.get("results") or []:
            if not isinstance(result, dict):
                continue
            ident = (str(result.get("project_id") or "unknown"),
                     str(result.get("model") or "unknown"))
            row = merged.setdefault((ident, index),
                                    {"index": index, "hour": hour_label(index),
                                     "requests": 0, "input": 0, "cached": 0})
            row["requests"] += _int(result.get("num_model_requests"))
            row["input"] += _int(result.get("input_tokens"))
            row["cached"] += _int(result.get("input_cached_tokens"))
    out = {}
    for (ident, _index), row in merged.items():
        if row["requests"] > 0 or row["input"] > 0:
            out.setdefault(ident, []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: r["index"])
    return out


def cached_share(rows):
    """Pooled cached share over a set of hours. Pure. None when nothing ran."""
    total = sum(_int(r.get("input")) for r in rows or [])
    if total <= 0:
        return None
    return sum(_int(r.get("cached")) for r in rows or []) / float(total)


def with_gaps(rows):
    """Annotate each hour with the idle hours immediately before it. Pure.

    The first row in the window is dropped rather than given a gap of zero.
    Nothing is visible before the window starts, so whether it resumed after
    idle time or continued from a busy hour is unknowable, and guessing either
    way biases the very comparison this note rests on.
    """
    ordered = sorted(rows or [], key=lambda r: _int(r.get("index")))
    out = []
    previous = None
    for row in ordered:
        index = _int(row.get("index"))
        if previous is not None:
            annotated = dict(row)
            annotated["gap"] = index - previous - 1
            out.append(annotated)
        previous = index
    return out


def gap_bin(gap):
    """Bucket a gap length into the band its repair belongs to. Pure."""
    gap = _int(gap)
    if gap <= 0:
        return "continuous"
    if gap == 1:
        return "1h"
    if gap <= 5:
        return "2-5h"
    if gap <= 23:
        return "6-23h"
    return "24h+"


def bin_shares(annotated):
    """Cached share per gap band. Pure. Returns {band: {hours, input, share}}.

    This is the finding's shape. Everything else in the section reads a ratio
    against time or against load; this reads it against how long the traffic
    had been away.
    """
    out = {}
    for row in annotated or []:
        band = gap_bin(row.get("gap"))
        cell = out.setdefault(band, {"hours": 0, "input": 0, "cached": 0})
        cell["hours"] += 1
        cell["input"] += _int(row.get("input"))
        cell["cached"] += _int(row.get("cached"))
    for cell in out.values():
        cell["share"] = (cell["cached"] / float(cell["input"])
                         if cell["input"] > 0 else None)
    return out


def collapse_bin(bands, cold_ceiling=0.05, min_hours=3):
    """The shortest gap at which the share has already gone. Pure. None if none.

    Shortest rather than worst on purpose. Everything caches badly after a
    week; what decides the repair is whether one idle hour was already enough,
    which is a retention default, or whether it takes a day, which is a
    schedule.
    """
    for band in BIN_ORDER:
        cell = (bands or {}).get(band)
        if not cell or cell.get("share") is None:
            continue
        if cell["hours"] >= min_hours and cell["share"] <= cold_ceiling:
            return band
    return None


def foregone_tokens(bands, warm_share):
    """Tokens that would have been cached at the warm rate. Pure.

    The money. Uncached input in the resumption hours priced against the share
    the same prefix achieves when traffic is continuous, which is the only
    honest benchmark available: it is this workload's own best hour, not a
    target borrowed from somewhere else.
    """
    if warm_share is None:
        return 0
    total = 0
    for band in BIN_ORDER:
        cell = (bands or {}).get(band)
        if not cell or cell.get("share") is None:
            continue
        total += int(max(0.0, warm_share - cell["share"]) * cell["input"])
    return total


def handoff(state):
    """Which note owns this shape, when it is not this one. Pure."""
    if state == "never-idle":
        return ("this series has no gaps at all, so eviction between runs "
                "cannot be the story. If the share is still low, read the "
                "prompt-cache-key-not-set note and check whether it degrades "
                "at peak instead.")
    if state == "cold-everywhere":
        return ("the continuously busy hours are cold too, so the prefix is "
                "not being matched even when the entry is certainly alive. "
                "Read cache-invalidated-by-changing-prefix, and "
                "prompt-below-model-cache-minimum if nothing caches at all.")
    return ""


def classify(rows, cold_ceiling=0.05, warm_floor=0.20, min_hours=24,
             min_band_hours=3):
    """Classify one project and model series. Pure. Returns (state, detail)."""
    annotated = with_gaps(rows)
    if len(annotated) < min_hours:
        return ("too-few-hours",
                "%d usable hour(s) after dropping the first, under the floor of "
                "%d" % (len(annotated), min_hours))

    bands = bin_shares(annotated)
    warm = bands.get("continuous") or {}
    warm_share = warm.get("share")
    idle_hours = sum(cell["hours"] for band, cell in bands.items()
                     if band != "continuous")

    if idle_hours < min_band_hours:
        return ("never-idle",
                "%d hour(s) of traffic and only %d of them resume after a gap"
                % (len(annotated), idle_hours))

    if warm_share is None or warm["hours"] < min_band_hours:
        return ("no-continuous-hours",
                "traffic never runs two hours back to back, so there is no warm "
                "baseline to compare a resumption against")

    if warm_share <= cold_ceiling:
        return ("cold-everywhere",
                "%.0f%% cached even in continuously busy hours" % (warm_share * 100))

    if warm_share < warm_floor:
        return ("warm-baseline-too-weak",
                "%.0f%% cached in continuously busy hours, under the floor of "
                "%.0f%%. The prefix is barely caching at the best of times, so "
                "the gaps are not the main story" % (warm_share * 100, warm_floor * 100))

    band = collapse_bin(bands, cold_ceiling, min_band_hours)
    if band is None:
        return ("warm-after-idle",
                "%.0f%% cached when continuous and no gap band has collapsed"
                % (warm_share * 100))

    cell = bands[band]
    return ("cold-after-idle",
            "%.0f%% cached in continuously busy hours and %.0f%% in the %d "
            "hour(s) that resume after a gap of %s. The prefix is fine; the "
            "entry is evicted while nobody is calling."
            % (warm_share * 100, cell["share"] * 100, cell["hours"], band))


def repair_lines(band, foregone):
    """The repair, keyed to how short a gap already loses the cache. Pure."""
    lines = []
    if band == "1h":
        lines.append("a single idle hour is already enough, so no retention "
                     "setting on offer covers it on its own: the 30m ttl "
                     "expires inside the gap.")
    elif band in ("2-5h", "6-23h"):
        lines.append("the cache survives a busy hour and not a gap of %s, which "
                     "is the default retention window doing exactly what it "
                     "says." % band)
    elif band == "24h+":
        lines.append("gaps of a day or more, which is the one case the 24h "
                     "retention option was added for.")
    lines.extend([
        "on models before GPT-5.6, set prompt_cache_retention=\"24h\" on this "
        "route. It is opt-in and costs nothing extra to set.",
        "on GPT-5.6 and later, set prompt_cache_options={\"ttl\": \"30m\"} "
        "explicitly so the retention is visible in the code rather than "
        "inherited, then check it against your actual gap length.",
        "reshape the schedule. Run intermittent work in one contiguous window "
        "instead of scattering it across the day, so the first call warms an "
        "entry the rest of the batch reads.",
        "about %d input token(s) in this window would have been cached at this "
        "workload's own continuous rate." % foregone,
    ])
    return lines


def window_start(days):
    """Floor to the hour so start_time lands on a bucket boundary."""
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return int((now - dt.timedelta(days=days)).timestamp())


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/ needs an admin key "
                         "(sk-admin-...), not a project key" % r.status_code)
    r.raise_for_status()
    return r.json()


def read_buckets(session, path, params):
    """Walk the paginated usage endpoint."""
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
    ap.add_argument("--days", type=int, default=14,
                    help="days of hourly buckets to read (max 30)")
    ap.add_argument("--cold-ceiling", type=float, default=0.05,
                    help="cached share at or below which a band counts as cold")
    ap.add_argument("--show-all", action="store_true",
                    help="also print series that are behaving")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key "
                  "(sk-admin-...); a project key cannot read /v1/organization/")
        return 2

    days = max(2, min(int(args.days), 30))
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + admin})

    buckets = read_buckets(session, "/organization/usage/completions", {
        "start_time": window_start(days),
        "bucket_width": "1h",
        "limit": 168,
        "group_by[]": ["project_id", "model"],
    })

    series = rows_by_series(buckets)
    if not series:
        log.info("no completions usage in the last %d day(s)", days)
        return 0

    checked = 0
    bad = 0
    for ident in sorted(series):
        rows = series[ident]
        state, detail = classify(rows, args.cold_ceiling)
        checked += 1
        line = "%-24s %s / %s  %s" % (state, ident[0], ident[1], detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            bands = bin_shares(with_gaps(rows))
            warm = (bands.get("continuous") or {}).get("share")
            band = collapse_bin(bands, args.cold_ceiling)
            for name in BIN_ORDER:
                cell = bands.get(name)
                if cell and cell.get("share") is not None:
                    log.warning("  after a gap of %-5s %d hour(s), %.0f%% cached",
                                name, cell["hours"], cell["share"] * 100)
            for repair in repair_lines(band, foregone_tokens(bands, warm)):
                log.warning("  repair: %s", repair)
            log.warning("  note: hourly buckets cannot see an idle stretch "
                        "shorter than an hour, so a gap band of 1h is a ceiling "
                        "on how quickly the entry actually went.")
        else:
            note = handoff(state)
            if note:
                log.info(line)
                log.info("  %s", note)
            elif args.show_all:
                log.info(line)

    log.info("%d project/model series checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
