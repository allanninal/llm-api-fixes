"""Report an Anthropic organization that never switched prompt caching on.

Read only. GET requests and nothing else against the Admin API, which needs an
Admin API key (sk-ant-admin...); a workspace key is rejected by every
/v1/organizations/* path, and an Admin key can be provisioned read-only. The
repair is printed, never performed: switching caching on is a change to your
own messages.create() call, not something a script should do to you.

Note on what this report can and cannot say: the messages usage report returns
token sums per bucket and carries no request count at all, so nothing here is
expressed per request. Every ratio below is a ratio of tokens.
"""
import argparse
import datetime
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_prompt_cache_off")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# Published multipliers on base input: a cache read is a tenth of the price of
# processing the same tokens uncached.
READ_MULTIPLIER = 0.10


def accumulate(results, into=None):
    """Sum the token fields that matter across usage-report results. Pure.

    cache_creation is a nested object holding ephemeral_5m_input_tokens and
    ephemeral_1h_input_tokens. A parser that looks for a flat field instead sums
    zero and reports a heavily cached organization as an uncached one, which is
    why this is a function with tests rather than four lines in a loop.
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


def cache_saving_ceiling(uncached_tokens, reusable_fraction):
    """Base-rate tokens you could stop paying for, at best. Pure.

    Deliberately a ceiling and not an estimate: it assumes the given fraction of
    uncached input is a stable prefix that would hit the cache every time, and
    prices that fraction at the read rate instead of the base rate. Real
    integrations do worse. Nothing in the API can tell you what the fraction
    actually is, because the API never returns your prompts.
    """
    if not 0.0 <= reusable_fraction <= 1.0:
        raise ValueError("reusable_fraction must be between 0 and 1")
    return int(max(0, uncached_tokens) * reusable_fraction * (1.0 - READ_MULTIPLIER))


def verdict(total, min_input=1_000_000):
    """Classify one workload's 30 day token totals. Pure.

    Returns (state, detail). The three states that matter are kept apart on
    purpose: caching absent, caching present, and not enough traffic to make
    either claim.
    """
    reads = int(total.get("cache_read", 0))
    writes = int(total.get("write_5m", 0)) + int(total.get("write_1h", 0))
    uncached = int(total.get("uncached", 0))

    if reads > 0:
        return ("in-use",
                "%.1fM read token(s) against %.1fM written. Caching is on here; "
                "whether it earns its keep is the write to read ratio, which is "
                "a separate question." % (reads / 1e6, writes / 1e6))
    if writes > 0:
        return ("writes-only",
                "%.1fM cache write token(s) and not one read. Caching is switched "
                "on and paying nothing back, which costs more than leaving it off: "
                "a write is 1.25x (5m) or 2x (1h) base input, an uncached call is "
                "1x." % (writes / 1e6))
    if uncached < min_input:
        return ("too-little-traffic",
                "only %d uncached input token(s) in the window; too little to "
                "conclude anything" % uncached)
    return ("never-used",
            "%.1fM uncached input token(s), zero cache reads and zero cache "
            "writes. Caching has never been switched on for this workload."
            % (uncached / 1e6))


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def buckets(session, path, params):
    """Walk the paginated usage or cost report."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def window_start(days):
    """Floor to midnight UTC, because starting_at must sit on a bucket boundary."""
    now = datetime.datetime.now(datetime.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="days of daily buckets to read")
    ap.add_argument("--min-input", type=int, default=1_000_000,
                    help="uncached input tokens below which no claim is made")
    ap.add_argument("--reusable", type=float, default=0.5,
                    help="fraction of input you believe is a stable prefix, used "
                         "only for the printed ceiling")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    params = {"starting_at": window_start(args.days), "bucket_width": "1d",
              "limit": min(args.days + 1, 31),
              "group_by[]": ["model", "workspace_id"]}

    workloads = {}
    for bucket in buckets(s, "/organizations/usage_report/messages", params):
        for result in bucket.get("results") or []:
            name = (result.get("model") or "all models",
                    result.get("workspace_id") or "default workspace")
            workloads[name] = accumulate([result], workloads.get(name))

    if not workloads:
        log.info("no message usage in the last %d day(s)", args.days)
        return 0

    off = 0
    for name, total in sorted(workloads.items(), key=lambda kv: -kv[1]["uncached"]):
        state, detail = verdict(total, args.min_input)
        line = "%-18s %s / %s  %s" % (state, name[0], name[1], detail)
        if state in ("in-use", "too-little-traffic"):
            log.info(line)
            continue
        off += 1
        log.warning(line)
        if state == "never-used":
            ceiling = cache_saving_ceiling(total["uncached"], args.reusable)
            log.warning("  at %.0f%% reusable prefix that is up to %.1fM base rate "
                        "input token(s) a window you would stop paying for",
                        args.reusable * 100, ceiling / 1e6)
            log.warning("  repair: add cache_control {\"type\": \"ephemeral\"} at the "
                        "end of the stable prefix, keep everything variable after "
                        "it, redeploy, then re-read this window tomorrow")
        else:
            log.warning("  repair: caching is already on here. Move the breakpoint "
                        "to the end of the stable prefix so entries get read back, "
                        "or remove it: paying to write and never read is worse "
                        "than not caching")

    log.info("%d workload(s), %d with caching switched off", len(workloads), off)
    return 1 if off else 0


if __name__ == "__main__":
    sys.exit(main())
