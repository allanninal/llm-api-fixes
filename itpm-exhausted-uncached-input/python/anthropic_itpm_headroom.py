"""Report an Anthropic input limiter that is full of uncached input.

Read only. Two GET requests and nothing else against the Admin API, which needs
an Admin API key (sk-ant-admin...); a workspace key is rejected by every
/v1/organizations/* path, and an Admin key can be provisioned read-only.

The repair is printed, never performed. Adding a cache_control breakpoint
changes what the model is shown on every request, which is a deploy.

The messages usage report carries token sums and no request count, so nothing
here is expressed per request. This script can say the input limiter is full.
It cannot say how many calls filled it.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_itpm_headroom")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# The one family where cache reads are charged against the input limiter. On
# every other current model a cache read is free of ITPM, which is the entire
# mechanism this script reports on, so the exception has to be explicit rather
# than a footnote in the prose.
CACHE_READS_CHARGED = ("claude-3-5-haiku",)

FINDINGS = ("itpm-saturated-uncached", "itpm-saturated-already-cached",
            "itpm-saturated-cache-counts")


def cache_reads_count(model):
    """Do cache reads count toward this model's ITPM? Pure.

    True only for Claude Haiku 3.5. Getting this backwards tells a reader to
    add a breakpoint that will not buy them a single token of headroom.
    """
    name = str(model or "").strip().lower()
    return any(name.startswith(prefix) for prefix in CACHE_READS_CHARGED)


def chargeable_input(result, model):
    """Tokens in one usage result that count against ITPM. Pure.

    cache_creation is a nested object holding ephemeral_5m_input_tokens and
    ephemeral_1h_input_tokens. A parser looking for a flat
    cache_creation_input_tokens sums zero and reports a heavily cached workload
    as one that writes nothing.
    """
    if not isinstance(result, dict):
        return 0
    total = 0
    for field in ("uncached_input_tokens",):
        try:
            total += int(result.get(field) or 0)
        except (TypeError, ValueError):
            pass
    creation = result.get("cache_creation") or {}
    for field in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
        try:
            total += int(creation.get(field) or 0)
        except (TypeError, ValueError):
            pass
    if cache_reads_count(model):
        try:
            total += int(result.get("cache_read_input_tokens") or 0)
        except (TypeError, ValueError):
            pass
    return total


def peaks(buckets):
    """Fold one-minute buckets into per-model peaks. Pure.

    The peak is the finding and the mean is not. ITPM is enforced by the
    minute, so a workload that saturates for ninety seconds an hour has a
    comfortable hourly average and a queue of 429s inside it.
    """
    per_minute = {}
    for bucket in buckets or []:
        stamp = str(bucket.get("starting_at") or bucket.get("start_time") or "")
        for result in bucket.get("results") or []:
            model = str(result.get("model") or "").strip() or "all models"
            row = per_minute.setdefault((model, stamp), {"charged": 0, "read": 0})
            row["charged"] += chargeable_input(result, model)
            try:
                row["read"] += int(result.get("cache_read_input_tokens") or 0)
            except (TypeError, ValueError):
                pass

    out = {}
    for (model, stamp), row in per_minute.items():
        stats = out.setdefault(model, {"peak": 0, "peak_at": None, "peak_read": 0,
                                       "minutes": 0, "charged": 0, "read": 0})
        stats["minutes"] += 1
        stats["charged"] += row["charged"]
        stats["read"] += row["read"]
        if row["charged"] > stats["peak"]:
            stats["peak"] = row["charged"]
            stats["peak_at"] = stamp
            stats["peak_read"] = row["read"]
    return out


def cache_read_share(stats, model):
    """Share of the peak minute's input that arrived as a cache read. Pure.

    On a model where reads are charged the peak already contains them, so the
    denominator differs. Using one denominator for both models reports the
    Haiku 3.5 case at half its real share.
    """
    if not isinstance(stats, dict):
        return None
    read = int(stats.get("peak_read") or 0)
    charged = int(stats.get("peak") or 0)
    total = charged if cache_reads_count(model) else charged + read
    if total <= 0:
        return None
    return min(1.0, read / float(total))


def headroom_multiplier(share):
    """How much total input one ITPM ceiling carries at a given read share. Pure.

    1 / (1 - share). At zero the ceiling carries exactly your uncached input;
    at 0.8 it carries five times your total input. This is the throughput
    argument for caching and it is a different argument from the discount.
    """
    if share is None:
        return None
    bounded = max(0.0, min(0.99, float(share)))
    return 1.0 / (1.0 - bounded)


def itpm_by_group(payload):
    """{model_group: input_tokens_per_minute} from the rate-limits response. Pure.

    A group whose limits[] omits the type is recorded as None. Absent means it
    inherits, never that it is unlimited, and reading a missing number as no
    ceiling is how a team decides it has room nobody granted it.
    """
    out = {}
    for entry in (payload or {}).get("data") or []:
        group = str(entry.get("model_group") or "").strip()
        if not group:
            continue
        out.setdefault(group, None)
        for limit in entry.get("limits") or []:
            if str(limit.get("type") or "").strip() != "input_tokens_per_minute":
                continue
            try:
                out[group] = int(limit.get("value"))
            except (TypeError, ValueError):
                out[group] = None
    return out


def limit_for(groups, model):
    """The ITPM for the group a model id belongs to, or None. Pure.

    Longest prefix wins, so a dated id resolves to the most specific group that
    claims it rather than to whichever one the dict happened to yield first.
    """
    name = str(model or "").strip().lower()
    if not name:
        return None
    best_key, best_len = None, -1
    for group in (groups or {}):
        candidate = str(group).strip().lower()
        if not candidate:
            continue
        if name == candidate or name.startswith(candidate):
            if len(candidate) > best_len:
                best_key, best_len = group, len(candidate)
    if best_key is None:
        return None
    return (groups or {}).get(best_key)


def verdict(model, stats, limit, floor=0.9, watch=0.6, min_minutes=10,
            cached_enough=0.15):
    """Classify one model's input limiter. Pure. Returns (state, detail).

    Three ways an ITPM ceiling can be full, and they do not share a repair:
    the prefix is uncached and caching buys headroom; the prefix is already
    cached and only a limit increase is left; or the model charges cache reads
    so caching was never going to buy headroom in the first place.
    """
    minutes = int((stats or {}).get("minutes") or 0)
    if minutes < min_minutes:
        return ("too-few-buckets",
                "%d minute(s) of traffic in the window, under the floor of %d. "
                "A peak taken over this little is noise." % (minutes, min_minutes))
    if limit is None or limit <= 0:
        return ("no-limit-published",
                "no input_tokens_per_minute is published for this model's group, "
                "so there is no ceiling to compare the peak against. The limiter "
                "still exists; the number was simply not returned.")

    peak = int(stats.get("peak") or 0)
    used = peak / float(limit)
    share = cache_read_share(stats, model)
    shape = ("peak minute charged %d token(s) against an ITPM of %d (%.0f%%); "
             "cache reads were %.0f%% of that minute's input"
             % (peak, limit, used * 100, (share or 0.0) * 100))

    if used < watch:
        return ("itpm-headroom", shape + ".")
    if used < floor:
        return ("itpm-approaching",
                shape + ". Thin enough that an ordinary spike lands on the "
                "input limiter rather than on the request limiter.")
    if cache_reads_count(model):
        return ("itpm-saturated-cache-counts",
                shape + ". This model charges cache reads against ITPM, so "
                "caching lowers the bill here and buys no headroom at all. The "
                "levers are a shorter prefix or a higher limit.")
    if share is not None and share >= cached_enough:
        return ("itpm-saturated-already-cached",
                shape + ". The prefix is already being read back, so a "
                "breakpoint has little left to give. What remains is a limit "
                "increase, or splitting the workload across model groups.")
    return ("itpm-saturated-uncached",
            shape + ". Cache reads are not charged against ITPM on this model, "
            "so covering the stable prefix buys throughput and not only a "
            "discount.")


def window_start(minutes):
    """Floor to the minute: starting_at must sit on a bucket boundary."""
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
    ap.add_argument("--minutes", type=int, default=240,
                    help="minutes of one-minute buckets to read (max 1440)")
    ap.add_argument("--target-share", type=float, default=0.8,
                    help="cache-read share to quote the multiplier at (default 0.8)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print models with headroom left")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    minutes = max(1, min(int(args.minutes), 1440))
    session = requests.Session()
    session.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    params = {"starting_at": window_start(minutes), "bucket_width": "1m",
              "limit": minutes, "group_by[]": ["model"]}
    stats = peaks(read_buckets(session, "/organizations/usage_report/messages", params))
    if not stats:
        log.info("no message usage in the last %d minute(s)", minutes)
        return 0

    groups = itpm_by_group(get(session, "/organizations/rate_limits"))

    checked = 0
    bad = 0
    for model in sorted(stats, key=lambda m: -stats[m]["peak"]):
        row = stats[model]
        limit = limit_for(groups, model)
        state, detail = verdict(model, row, limit)
        checked += 1
        line = "%-30s %-28s %s" % (state, model, detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            if state == "itpm-saturated-uncached":
                share = cache_read_share(row, model) or 0.0
                now_x = headroom_multiplier(share)
                then_x = headroom_multiplier(args.target_share)
                log.warning("  at this read share the ceiling carries %.1fx your "
                            "total input; at %.0f%% it would carry %.1fx",
                            now_x, args.target_share * 100, then_x)
                log.warning("  repair: put a cache_control breakpoint at the end "
                            "of the stable prefix. The render order is tools, "
                            "then system, then messages, so the breakpoint goes "
                            "after the last thing that never changes.")
            else:
                log.warning("  repair: request an input_tokens_per_minute "
                            "increase for this model group, or move latency "
                            "tolerant work onto the Message Batches API, which "
                            "is metered by its own limiter group.")
        elif state in ("itpm-approaching", "no-limit-published"):
            log.warning(line)
        elif args.show_all:
            log.info(line)

    log.info("%d model(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
