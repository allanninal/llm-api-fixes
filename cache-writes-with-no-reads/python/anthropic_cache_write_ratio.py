"""Report Anthropic cache writes that are never read back.

Read only. GET requests and nothing else against the Admin API, which needs an
Admin API key (sk-ant-admin...); a workspace key is rejected by every
/v1/organizations/* path, and an Admin key can be provisioned read-only. The
repair is printed, never performed: moving a cache_control breakpoint is a
change to your own request, not something a script should do to you.

The messages usage report carries token sums per bucket and no request count at
all, so "reads per write" below means read tokens per write token. It is a
proxy for call counts, not a call count, and this API has no call count to
check it against.
"""
import argparse
import datetime
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_cache_write_ratio")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# Published multipliers on base input.
WRITE_5M = 1.25
WRITE_1H = 2.00
READ = 0.10
BASE = 1.00


def accumulate(results, into=None):
    """Sum token fields across usage-report results, keeping the TTLs apart. Pure.

    The two cache_creation members are priced differently, so summing them here
    would throw away the information the break-even calculation needs. They live
    inside a nested cache_creation object, which is the field a flat parser
    misses entirely.
    """
    total = {"uncached": 0, "cache_read": 0, "write_5m": 0, "write_1h": 0}
    if into:
        total.update(into)
    for result in results or []:
        total["uncached"] += int(result.get("uncached_input_tokens") or 0)
        total["cache_read"] += int(result.get("cache_read_input_tokens") or 0)
        creation = result.get("cache_creation") or {}
        total["write_5m"] += int(creation.get("ephemeral_5m_input_tokens") or 0)
        total["write_1h"] += int(creation.get("ephemeral_1h_input_tokens") or 0)
    return total


def break_even_ratio(write_5m, write_1h):
    """Read tokens per write token at which caching starts to save money. Pure.

    Caching w write tokens and r read tokens costs 1.25*w5 + 2.0*w1h + 0.1*r,
    against w5 + w1h + r for the same tokens uncached. Solving for r gives
    r > ((1.25-1)*w5 + (2.0-1)*w1h) / (1 - 0.1), which is about 0.28 for pure
    5m traffic and about 1.11 for pure 1h. Returns None when nothing was
    written, because a ratio against zero is not a number.
    """
    writes = write_5m + write_1h
    if writes <= 0:
        return None
    premium = (WRITE_5M - BASE) * write_5m + (WRITE_1H - BASE) * write_1h
    return premium / ((BASE - READ) * writes)


def effective_multiplier(write_5m, write_1h, reads):
    """What this cached traffic costs per token relative to not caching. Pure.

    Above 1.0 means the caching is charging you a surcharge: the same tokens
    would have been cheaper with the feature switched off.
    """
    tokens = write_5m + write_1h + reads
    if tokens <= 0:
        return None
    cost = WRITE_5M * write_5m + WRITE_1H * write_1h + READ * reads
    return cost / tokens


def verdict(total, min_writes=100_000, margin=1.5):
    """Classify one key's cache economics over the window. Pure.

    Returns (state, detail). `margin` is how far above break-even a ratio has to
    sit before it is called safe rather than marginal, because a ratio sitting
    on the line will cross it the first week traffic dips.
    """
    reads = int(total.get("cache_read", 0))
    write_5m = int(total.get("write_5m", 0))
    write_1h = int(total.get("write_1h", 0))
    writes = write_5m + write_1h

    if writes == 0 and reads == 0:
        return ("no-caching",
                "no cache reads and no cache writes in this window: caching is "
                "not switched on for this key at all, which is a different "
                "problem from this one")
    if writes == 0:
        return ("reads-only",
                "%d read token(s) against entries written before this window "
                "opened. Widen the window before drawing a ratio from it." % reads)
    if writes < min_writes:
        return ("too-little-traffic",
                "only %d cache write token(s) in the window; too little to draw "
                "a ratio from" % writes)

    ratio = reads / writes
    threshold = break_even_ratio(write_5m, write_1h)
    multiplier = effective_multiplier(write_5m, write_1h, reads)
    shape = ("%.2f read tokens per write token against a break-even of %.2f; "
             "this traffic costs %.2fx what the same tokens would cost with "
             "caching switched off" % (ratio, threshold, multiplier))
    if ratio < threshold:
        return ("losing", shape)
    if ratio < threshold * margin:
        return ("marginal", shape + ", which is barely above the line")
    return ("paying-off", shape)


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def buckets(session, path, params):
    params = dict(params)
    while True:
        page = get(session, path, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def window_start(days):
    """Floor to the hour, because starting_at must sit on a bucket boundary."""
    now = datetime.datetime.now(datetime.timezone.utc)
    top = now.replace(minute=0, second=0, microsecond=0)
    return (top - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="days of hourly buckets to read")
    ap.add_argument("--min-writes", type=int, default=100_000,
                    help="cache write tokens below which no ratio is claimed")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    params = {"starting_at": window_start(args.days), "bucket_width": "1h",
              "limit": min(args.days * 24, 168), "group_by[]": ["api_key_id"]}

    by_key = {}
    for bucket in buckets(s, "/organizations/usage_report/messages", params):
        for result in bucket.get("results") or []:
            name = result.get("api_key_id") or "unattributed"
            by_key[name] = accumulate([result], by_key.get(name))

    if not by_key:
        log.info("no message usage in the last %d day(s)", args.days)
        return 0

    losing = 0
    for name, total in sorted(by_key.items(),
                              key=lambda kv: -(kv[1]["write_5m"] + kv[1]["write_1h"])):
        state, detail = verdict(total, args.min_writes)
        line = "%-18s %s  %s" % (state, name, detail)
        if state in ("paying-off", "too-little-traffic", "reads-only"):
            log.info(line)
            continue
        if state == "no-caching":
            log.info(line)
            continue
        losing += 1
        log.warning(line)
        log.warning("  repair: move the cache_control breakpoint to the end of the "
                    "stable prefix and keep timestamps, request ids and the user's "
                    "question strictly after it, then re-measure this ratio tomorrow")
        if total["write_1h"] > total["write_5m"]:
            log.warning("  note: most writes here are 1h entries at 2x base input, "
                        "so break-even needs about twice the reads a 5m entry does")
        log.warning("  confirm in money: GET %s/organizations/cost_report"
                    "?starting_at=<T-30d>&group_by[]=description", API)

    log.info("%d key(s), %d losing money on caching", len(by_key), losing)
    return 1 if losing else 0


if __name__ == "__main__":
    sys.exit(main())
