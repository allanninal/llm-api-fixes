"""Find OpenAI traffic whose cached share falls as concurrency rises.

Read only. One paginated GET against the Usage API, which needs an admin key
(sk-admin-...); a project key is rejected by every /v1/organization/ path, and
an admin key can be provisioned with the read scopes only.

Cache lookup is prefix-based and routing-sensitive. Without prompt_cache_key a
fleet sprays byte-identical prompts across many backends and each one sees a
cold prefix, so the hit rate gets *worse* as you scale out. That is the whole
signature: a cached share that is negatively correlated with the hour's request
count. A prefix that is simply unstable produces a flat low share at every load
and belongs to a different note.

Hours that follow a gap in traffic are dropped before anything is correlated.
Those hours run cold because the entry was evicted while nobody was calling,
which is a third note again, and leaving them in would manufacture exactly the
negative correlation this one is looking for.

The repair is printed, never performed.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_cache_key_routing_scatter")

API = "https://api.openai.com/v1"

FINDINGS = ("load-correlated-misses",)


def _int(value):
    """Read a usage field as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def hour_index(stamp):
    """Hours since the epoch. Pure. None if unreadable.

    The usage buckets carry start_time as a unix integer, but the same code has
    to survive an ISO string, and adjacency has to be integer arithmetic: gap
    detection on formatted timestamps gets 23:00 and 00:00 wrong every night.
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
    """Per (project_id, model), one row per active hour, sorted. Pure."""
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
    """Pooled cached share over a set of hours. Pure. None when nothing ran.

    Pooled rather than averaged: an hour with nine requests must not carry the
    same weight as an hour with nine thousand, which is the entire quantity
    under test here.
    """
    total = sum(_int(r.get("input")) for r in rows or [])
    if total <= 0:
        return None
    return sum(_int(r.get("cached")) for r in rows or []) / float(total)


def continuation_rows(rows):
    """Hours whose previous hour also carried traffic. Pure.

    The exclusion that keeps this note off someone else's ground. An hour that
    follows idle time starts from an evicted cache no matter how the requests
    were routed, so it cannot be evidence about routing.
    """
    active = {_int(r.get("index")) for r in rows or []}
    return [r for r in rows or [] if _int(r.get("index")) - 1 in active]


def resumption_rows(rows):
    """The hours the correlation deliberately threw away. Pure."""
    active = {_int(r.get("index")) for r in rows or []}
    return [r for r in rows or [] if _int(r.get("index")) - 1 not in active]


def _ranks(values):
    """Average ranks, ties shared. Pure."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Rank correlation between two equal-length series. Pure. None if flat.

    Rank rather than Pearson because request counts are heavy tailed and one
    incident hour would otherwise decide the answer.

    The two degenerate cases are deliberately different answers. A load that
    never varies returns None, because nothing at all can be said about
    concurrency from a flat request rate. A share that never varies returns
    0.0, because "no relationship" is a real finding here and it is the one
    that sends the reader to the prefix-instability note.
    """
    xs = list(xs or [])
    ys = list(ys or [])
    if len(xs) != len(ys) or len(xs) < 8:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = float(len(xs))
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0:
        return None
    if syy <= 0:
        return 0.0
    return sxy / ((sxx ** 0.5) * (syy ** 0.5))


def load_split(rows, fraction=0.33):
    """Pooled cached share in the quietest and busiest hours. Pure.

    Returns (quiet_share, busy_share, quiet_rate, busy_rate) or Nones. The
    correlation says the relationship is monotone; this says how big it is in
    the only units anyone will act on, which is the discount you are not getting
    at peak.
    """
    active = [r for r in rows or [] if _int(r.get("requests")) > 0]
    if len(active) < 6:
        return (None, None, None, None)
    ordered = sorted(active, key=lambda r: _int(r.get("requests")))
    size = max(2, int(len(ordered) * fraction))
    quiet, busy = ordered[:size], ordered[-size:]

    def rate(part):
        return sum(_int(r.get("requests")) for r in part) / float(len(part))

    return (cached_share(quiet), cached_share(busy), rate(quiet), rate(busy))


def handoff(state):
    """Which note owns this shape, when it is not this one. Pure."""
    if state == "no-cached-tokens":
        return ("not one cached token at any load, so the traffic never becomes "
                "eligible rather than being routed away from its cache. Read the "
                "prompt-below-model-cache-minimum note and check the mean input "
                "per request against the model's floor first.")
    if state == "flat-low-share":
        return ("the share is low and stays low whatever the load, which is a "
                "prefix that differs between calls rather than requests landing "
                "on different machines. Read the "
                "cache-invalidated-by-changing-prefix note.")
    if state == "cold-only-after-idle":
        return ("the cold hours are the ones that follow gaps in traffic, and "
                "the busy hours are fine. That is eviction during idle time: "
                "read the prompt-cache-retention-left-at-default note.")
    return ""


def classify(rows, rho_floor=-0.4, ratio_floor=0.6, quiet_floor=0.15,
             min_hours=24):
    """Classify one project and model series. Pure. Returns (state, detail).

    Only a series whose cached share falls monotonically as the hour gets busier
    belongs to this note, and only after the post-gap hours have been removed.
    """
    rows = rows or []
    linked = continuation_rows(rows)
    if len(linked) < min_hours:
        return ("too-few-linked-hours",
                "%d hour(s) with traffic in the hour before them, under the "
                "floor of %d. Correlating against load needs a run of busy "
                "hours." % (len(linked), min_hours))

    overall = cached_share(linked)
    if overall is not None and overall <= 0.0:
        return ("no-cached-tokens",
                "%d input token(s) across %d linked hour(s) and not one cached"
                % (sum(_int(r.get("input")) for r in linked), len(linked)))

    quiet, busy, quiet_rate, busy_rate = load_split(linked)
    rho = spearman([_int(r.get("requests")) for r in linked],
                   [cached_share([r]) or 0.0 for r in linked])

    if rho is None or quiet is None or busy is None:
        return ("load-does-not-vary",
                "the request rate barely moves across the window, so nothing "
                "here can be attributed to concurrency")

    if rho <= rho_floor and quiet >= quiet_floor and busy <= quiet * ratio_floor:
        return ("load-correlated-misses",
                "cached share %.0f%% in the quietest hours (%.0f req/h) and "
                "%.0f%% in the busiest (%.0f req/h), rank correlation %.2f "
                "against request rate. The prefix is cacheable; the requests "
                "are not landing where it is cached."
                % (quiet * 100, quiet_rate, busy * 100, busy_rate, rho))

    if rho >= -rho_floor:
        return ("share-rises-with-load",
                "cached share climbs with the request rate (%.2f): density is "
                "keeping entries warm, which is the opposite of scatter" % rho)

    cold = resumption_rows(rows)
    cold_share = cached_share(cold)
    if (overall is not None and cold_share is not None and cold_share <= 0.02
            and overall >= quiet_floor and len(cold) >= 3):
        return ("cold-only-after-idle",
                "%.0f%% cached in linked hours against %.0f%% in the %d hour(s) "
                "that follow a gap" % (overall * 100, cold_share * 100, len(cold)))

    if overall is not None and overall < quiet_floor:
        return ("flat-low-share",
                "cached share %.0f%% overall with rank correlation %.2f against "
                "load: low everywhere rather than low under load"
                % (overall * 100, rho))

    return ("healthy",
            "cached share %.0f%% quiet and %.0f%% busy, correlation %.2f"
            % (quiet * 100, busy * 100, rho))


def repair_lines():
    """The routing hint, and what makes a good one. Pure."""
    return [
        "set prompt_cache_key on the route: "
        "client.responses.create(..., prompt_cache_key=\"rag-answer-v3\").",
        "make it coarse. The template name, or the template plus tenant, so "
        "traffic concentrates on a few caches. A per-request id scatters the "
        "fleet exactly as badly as no key at all.",
        "keep it out of the prompt. It is a routing hint, not content, and it "
        "does not pin a request to a machine or guarantee a hit.",
        "then re-read these same hourly buckets. What should move first is the "
        "busy end: the gap between the quiet and busy shares closes before the "
        "average does.",
    ]


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
    ap.add_argument("--days", type=int, default=7,
                    help="days of hourly buckets to read (max 30)")
    ap.add_argument("--rho-floor", type=float, default=-0.4,
                    help="rank correlation at or below which the share is "
                         "treated as load-correlated")
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
        state, detail = classify(rows, args.rho_floor)
        checked += 1
        line = "%-24s %s / %s  %s" % (state, ident[0], ident[1], detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            log.warning("  %d hour(s) that follow a gap in traffic were excluded "
                        "before correlating; those run cold for a different "
                        "reason.", len(resumption_rows(rows)))
            for repair in repair_lines():
                log.warning("  repair: %s", repair)
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
