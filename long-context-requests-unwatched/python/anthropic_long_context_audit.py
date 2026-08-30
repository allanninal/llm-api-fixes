"""Report Claude workloads whose input has grown into the 200k-1M band.

Read only. GET requests and nothing else: ANTHROPIC_ADMIN_KEY must be an Admin
API key (sk-ant-admin...), which can be provisioned read-only. A workspace key
is rejected by every /v1/organizations/* path.

This is a SIZE alarm and not a price alarm. On current models the 1M context
window is the default, no beta header is involved, and long-context requests
bill at standard rates. The old belief that crossing 200k triggers premium
pricing came from a retired beta and is not true now.

What the band measures is a prefix that grows every turn: expensive because it
is enormous at an ordinary rate, and inaccurate because the window fills. The
repair is compaction first and caching second, and it is printed, because both
are application changes.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_long_context_audit")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

SHORT_BAND = "0-200k"
LONG_BAND = "200k-1M"
UNBANDED = "unbanded"

FINDINGS = ("long-context-uncached",)


def band(result):
    """Normalise the context_window value. Pure.

    A null becomes "unbanded", never "0-200k". Folding unbanded traffic into the
    short band deflates the long share and turns a real finding into a
    comfortable number, which is the one outcome this whole check exists to
    prevent.
    """
    raw = str((result or {}).get("context_window") or "").strip().lower()
    if raw == LONG_BAND.lower():
        return LONG_BAND
    if raw == SHORT_BAND.lower():
        return SHORT_BAND
    return UNBANDED


def fold(pages):
    """Sum input tokens into {model: {band: {uncached, cache_read}}}. Pure."""
    out = {}
    for page in pages:
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                model = str(result.get("model") or "all models")
                where = band(result)
                row = out.setdefault(model, {}).setdefault(
                    where, {"uncached": 0, "cache_read": 0})
                for field, key in (("uncached_input_tokens", "uncached"),
                                   ("cache_read_input_tokens", "cache_read")):
                    try:
                        row[key] += int(result.get(field) or 0)
                    except (TypeError, ValueError):
                        pass
    return out


def long_share(model_rows):
    """Share of BANDED uncached input sitting in the 200k-1M band. Pure.

    Banded only. Unbanded traffic cannot be placed on either side, and putting
    it in the denominator would make a workload look shorter than it is purely
    because the report declined to classify some of it.
    """
    rows = model_rows or {}
    short = int((rows.get(SHORT_BAND) or {}).get("uncached") or 0)
    long_ = int((rows.get(LONG_BAND) or {}).get("uncached") or 0)
    banded = short + long_
    if banded <= 0:
        return 0.0
    return long_ / float(banded)


def cached_share(row):
    """Share of a band's input that was read back from cache. Pure.

    Grades severity, not diagnosis: a cached long prefix costs a tenth as much
    and is exactly as long, so it fixes the money and none of the accuracy.
    """
    data = row or {}
    reads = int(data.get("cache_read") or 0)
    uncached = int(data.get("uncached") or 0)
    total = reads + uncached
    if total <= 0:
        return 0.0
    return reads / float(total)


def uncached_cost(tokens, rate_per_mtok):
    """Dollars for a number of uncached input tokens. Pure.

    The rate is passed in rather than baked into a table. A price table in an
    audit script is a fact with an expiry date on it, and nothing warns you the
    day it passes.
    """
    if rate_per_mtok < 0:
        raise ValueError("rate_per_mtok must not be negative")
    return max(0, int(tokens or 0)) / 1e6 * float(rate_per_mtok)


def verdict(model_rows, min_tokens=10_000_000, long_threshold=0.25,
            cache_floor=0.30):
    """Classify one model's context profile. Pure. Returns (state, detail)."""
    rows = model_rows or {}
    banded = sum(int((rows.get(b) or {}).get("uncached") or 0)
                 for b in (SHORT_BAND, LONG_BAND))
    unbanded = int((rows.get(UNBANDED) or {}).get("uncached") or 0)
    total = banded + unbanded

    if total < min_tokens:
        return ("low-volume",
                "%d uncached input token(s) in the window, too few to conclude "
                "anything" % total)
    if banded <= 0:
        return ("unbanded-only",
                "%.1fM uncached input token(s) with no context_window on any "
                "result, so this traffic cannot be placed in a band at all"
                % (unbanded / 1e6))

    share = long_share(rows)
    long_row = rows.get(LONG_BAND) or {}
    cached = cached_share(long_row)
    shape = ("%.0f%% of banded uncached input is %s, with %.0f%% of that band "
             "read from cache" % (share * 100, LONG_BAND, cached * 100))

    if share < long_threshold:
        return ("short-context",
                "%s. The prefix is not where the money is going here." % shape)
    if cached >= cache_floor:
        return ("long-context-cached",
                "%s. The big prefix is being read back rather than reprocessed, "
                "so it costs a tenth of full rate. It is still just as long, "
                "and length is what degrades the answer." % shape)
    return ("long-context-uncached",
            "%s. A very large prefix reprocessed from scratch on every call. "
            "Standard rates, extraordinary volume." % shape)


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params):
    """Walk the paginated usage report."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        yield page
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def window_start(days):
    """Floor to midnight UTC: starting_at must sit on a bucket boundary."""
    now = dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of daily buckets to read (default 30)")
    ap.add_argument("--input-rate", type=float, default=5.0,
                    help="dollars per million uncached input tokens, for the "
                         "printed estimate only (default 5.0)")
    ap.add_argument("--min-tokens", type=int, default=10_000_000,
                    help="uncached input tokens below which no claim is made")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    rows = fold(pages(s, "/organizations/usage_report/messages",
                      {"starting_at": window_start(args.days),
                       "bucket_width": "1d", "limit": min(args.days + 1, 31),
                       "group_by[]": ["context_window", "model"]}))

    checked = 0
    bad = 0
    for model in sorted(rows, key=lambda m: -((rows[m].get(LONG_BAND) or {})
                                              .get("uncached") or 0)):
        state, detail = verdict(rows[model], args.min_tokens)
        checked += 1
        line = "%-22s %-22s %s" % (state, model, detail)

        if state == "long-context-cached":
            log.warning(line)
            log.warning("  note: caching fixed the price and not the length. "
                        "Compaction is still the lever for answer quality.")
            continue
        if state not in FINDINGS:
            log.info(line)
            continue

        bad += 1
        log.warning(line)
        tokens = (rows[model].get(LONG_BAND) or {}).get("uncached") or 0
        log.warning("  %.1fM uncached token(s) in the band, about $%.2f at "
                    "$%.2f per million", tokens / 1e6,
                    uncached_cost(tokens, args.input_rate), args.input_rate)
        log.warning("  repair: compact or edit the context on the routes "
                    "generating 200k+ prefixes, then put a cache_control "
                    "breakpoint on whatever stays stable. In that order.")
        log.warning("  note: this band is not a premium price tier. It is "
                    "standard rates on a very large number of tokens.")

    unbanded = sum((rows[m].get(UNBANDED) or {}).get("uncached") or 0 for m in rows)
    if unbanded:
        log.info("%.1fM uncached token(s) carried no context_window and were "
                 "excluded from every share above", unbanded / 1e6)

    log.info("%d model(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
