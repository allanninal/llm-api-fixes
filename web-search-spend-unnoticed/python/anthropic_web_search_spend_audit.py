"""Report the per-search tool fee Claude web search is adding to the bill.

Read only. GET requests and nothing else: ANTHROPIC_ADMIN_KEY must be an Admin
API key (sk-ant-admin...), which can be provisioned read-only. A workspace key
is rejected by every /v1/organizations/* path.

This fee is not a token price. Web search bills $10 per 1,000 searches on top of
whatever the tokens cost, so no graph built on input and output tokens can show
it however carefully it is drawn.

The repair is printed, never applied. A max_uses cap and an allowed_domains
narrowing change what the agent is able to answer, and that belongs to whoever
owns the product, not to an audit holding an admin key.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_web_search_spend_audit")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# $10 per 1,000 searches, charged per search regardless of how many results come
# back. The unit is in the name because the natural slip is to multiply by ten
# and quote a bill a thousand times too large.
FEE_PER_THOUSAND = 10.0

# The cost report's own name for the row. Money for a server tool does not
# arrive as a token_type; it arrives under its own cost_type.
COST_TYPE = "web_search"

FINDINGS = ("search-fee",)


def fold(pages):
    """Sum server tool invocations per API key. Pure.

    server_tool_use is a nested object sitting beside the token fields, not one
    of them. Walking the result flat finds nothing and reports an organization
    running a million searches a month as running none.

    Counters other than web_search_requests are kept under their own names
    rather than dropped. New server tools ship, and a script that sums only the
    field it was written for keeps printing the same reassuring number after the
    next billable one arrives.
    """
    out = {}
    for page in pages:
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                key = str(result.get("api_key_id") or "unattributed")
                row = out.setdefault(key, {"web_search": 0, "other_tools": {}})
                use = result.get("server_tool_use")
                if not isinstance(use, dict):
                    continue
                for name, value in use.items():
                    try:
                        count = int(value or 0)
                    except (TypeError, ValueError):
                        continue
                    if count <= 0:
                        continue
                    if name == "web_search_requests":
                        row["web_search"] += count
                    else:
                        row["other_tools"][name] = row["other_tools"].get(name, 0) + count
    return out


def fee(searches, per_thousand=FEE_PER_THOUSAND):
    """Dollars owed for a number of searches. Pure."""
    try:
        n = int(searches or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0, n) * per_thousand / 1000.0


def search_spend(cost_buckets, cost_type=COST_TYPE):
    """Sum the cost report rows the platform itself calls web search. Pure.

    amount arrives as a decimal string, not a number. Summing the raw values
    concatenates them in one language and throws in the other, and the failure
    is quiet enough to ship.
    """
    total = 0.0
    for bucket in cost_buckets or []:
        for result in bucket.get("results") or []:
            if str(result.get("cost_type") or "") != cost_type:
                continue
            raw = result.get("amount")
            if raw is None or raw == "":
                continue
            try:
                total += float(raw)
            except (TypeError, ValueError):
                pass
    return total


def verdict(row, min_searches=100):
    """Classify one key's search volume. Pure. Returns (state, detail).

    The floor exists because a handful of searches is a demo, not a bill, and a
    finding printed against it costs the reader more attention than it saves
    them money.
    """
    searches = int((row or {}).get("web_search") or 0)
    if searches <= 0:
        return ("no-searches", "the web search tool was never invoked by this key")
    if searches < min_searches:
        return ("low-volume",
                "%d search(es), under the floor of %d, worth about $%.2f"
                % (searches, min_searches, fee(searches)))
    return ("search-fee",
            "%d search(es) at $%.0f per 1,000, a tool fee of about $%.2f before "
            "a single token is priced"
            % (searches, FEE_PER_THOUSAND, fee(searches)))


def reconcile(estimate, billed, tolerance=0.25):
    """Compare the estimate against what was actually charged. Pure.

    Four states, not two, because the ways these two numbers can disagree have
    different explanations. A search that errors is counted as a use and not
    billed, so the estimate may legitimately run ahead. The cost report also
    lags. Neither is a licence to present one number as the other.
    """
    if estimate <= 0 and billed <= 0:
        return ("no-searches", "no searches counted and no web_search row billed")
    if billed <= 0:
        return ("unpriced",
                "$%.2f of searches counted and no web_search row on the cost "
                "report. Either the report has not caught up with the window, "
                "or the searches errored and were never billed." % estimate)
    if estimate <= 0:
        return ("billed-without-count",
                "$%.2f billed as web_search with no searches counted. The two "
                "reports are not covering the same days." % billed)
    drift = abs(billed - estimate) / estimate
    if drift <= tolerance:
        return ("confirmed",
                "$%.2f billed against $%.2f estimated, within %.0f%%"
                % (billed, estimate, tolerance * 100))
    return ("mismatch",
            "$%.2f billed against $%.2f estimated, %.0f%% apart. Read the "
            "web_search rows directly before quoting either number."
            % (billed, estimate, drift * 100))


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params):
    """Walk the paginated usage or cost report."""
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
    ap.add_argument("--min-searches", type=int, default=100,
                    help="searches below which no claim is made (default 100)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print keys that never used the tool")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    start = window_start(args.days)
    rows = fold(pages(s, "/organizations/usage_report/messages",
                      {"starting_at": start, "bucket_width": "1d",
                       "limit": min(args.days + 1, 31),
                       "group_by[]": ["api_key_id"]}))

    cost_buckets = []
    for page in pages(s, "/organizations/cost_report",
                      {"starting_at": start, "limit": 31,
                       "group_by[]": ["description"]}):
        cost_buckets.extend(page.get("data") or [])

    checked = 0
    bad = 0
    estimate = 0.0
    for key in sorted(rows, key=lambda k: -rows[k]["web_search"]):
        row = rows[key]
        state, detail = verdict(row, args.min_searches)
        checked += 1
        estimate += fee(row["web_search"])
        line = "%-14s %-14s %s" % (state, key, detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            log.warning("  repair: set max_uses on the web search tool "
                        "definition for this service and narrow allowed_domains "
                        "to the hosts its answers actually cite")
            log.warning("  note: search results also re-enter input tokens on "
                        "every later turn of the same conversation, which is a "
                        "second charge this fee does not include")
        elif state == "low-volume" or args.show_all:
            log.info(line)

        for name, count in sorted(row["other_tools"].items()):
            log.info("  other server tool %s: %d use(s) by %s", name, count, key)

    billed = search_spend(cost_buckets)
    state, detail = reconcile(estimate, billed)
    (log.info if state in ("confirmed", "no-searches") else log.warning)(
        "%-14s %s", state, detail)

    log.info("%d key(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
