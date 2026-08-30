"""Report Claude traffic paying the US inference geo premium.

Read only. GET requests and nothing else: ANTHROPIC_ADMIN_KEY must be an Admin
API key (sk-ant-admin...), which can be provisioned read-only. A workspace key
is rejected by every /v1/organizations/* path.

inference_geo "us" multiplies every token pricing category by 1.1 on Claude 4.6
and later. The parameter is usually not chosen per request: it is inherited from
a workspace's data_residency.default_inference_geo, which means the premium is
paid by traffic whose callers never asked for it.

The repair is printed, never applied. Data residency is a compliance setting.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_inference_geo_premium_audit")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# 1.1x on every token pricing category: input, output, cache writes and cache
# reads alike. Caching does not dilute it, because the cache rates move too.
GEO_MULTIPLIER = 1.1

# Every token field the multiplier touches. cache_creation is nested, and a flat
# read of it sums zero and understates a heavily cached workspace.
FLAT_TOKEN_FIELDS = ("uncached_input_tokens", "output_tokens",
                     "cache_read_input_tokens")
NESTED_TOKEN_FIELDS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")

FINDINGS = ("us-by-workspace-default", "us-by-request", "us-unexplained")


def geo_of(result):
    """Normalise the inference_geo value. Pure.

    A null becomes "unspecified" and never "global". They are different facts:
    one is traffic served globally, the other is traffic the report declined to
    place, and quietly merging them flatters the share in the wrong direction.
    """
    raw = str((result or {}).get("inference_geo") or "").strip().lower()
    if raw in ("us", "global", "not_available"):
        return raw
    return "unspecified"


def tokens_of(result):
    """Sum every priced token category on one usage result. Pure.

    All of them, because the multiplier applies to all of them. cache_creation
    is a nested object; reading it as a number is how a cached workspace comes
    out looking small.
    """
    total = 0
    for field in FLAT_TOKEN_FIELDS:
        try:
            total += int((result or {}).get(field) or 0)
        except (TypeError, ValueError):
            pass
    creation = (result or {}).get("cache_creation")
    if isinstance(creation, dict):
        for field in NESTED_TOKEN_FIELDS:
            try:
                total += int(creation.get(field) or 0)
            except (TypeError, ValueError):
                pass
    return total


def fold(pages):
    """Sum priced tokens into {workspace_id: {geo: tokens}}. Pure."""
    out = {}
    for page in pages:
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                workspace = str(result.get("workspace_id") or "default workspace")
                geo = geo_of(result)
                per_geo = out.setdefault(workspace, {})
                per_geo[geo] = per_geo.get(geo, 0) + tokens_of(result)
    return out


def us_share(geo_totals):
    """The share of priced tokens served on inference_geo us. Pure."""
    total = sum(int(v or 0) for v in (geo_totals or {}).values())
    if total <= 0:
        return 0.0
    return int((geo_totals or {}).get("us") or 0) / float(total)


def premium_estimate(billed_dollars, share, multiplier=GEO_MULTIPLIER):
    """Back the premium out of an amount that already contains it. Pure.

    NOT billed * share * 0.1. The billed figure is already 1.1x the base rate,
    so the premium is (m - 1) / m of it, about 9.09%. Adding the multiplier on
    instead of removing it overstates the saving by a tenth, in the one sentence
    somebody is going to quote at whoever owns the budget.

    Assumes the token mix is roughly the same across geos inside one workspace,
    which is an approximation and is labelled as one wherever it is printed.
    """
    if multiplier <= 1.0:
        return 0.0
    dollars = max(0.0, float(billed_dollars or 0.0))
    fraction = min(1.0, max(0.0, float(share or 0.0)))
    return dollars * fraction * (multiplier - 1.0) / multiplier


def residency_default(workspace):
    """A workspace's configured default inference geo. Pure.

    Returns "us", "global", "not_available" or "unset". "unset" covers both a
    missing data_residency block and one this script cannot read, because the
    repair is the same in either case: go and look at the workspace.
    """
    block = (workspace or {}).get("data_residency")
    if not isinstance(block, dict):
        return "unset"
    value = str(block.get("default_inference_geo") or "").strip().lower()
    return value if value in ("us", "global", "not_available") else "unset"


def verdict(geo_totals, default_geo, min_tokens=1_000_000):
    """Classify one workspace. Pure. Returns (state, detail).

    A workspace default and an explicit per-request parameter are kept apart
    deliberately. The premium is identical; the owner of the fix is not.
    """
    totals = geo_totals or {}
    total = sum(int(v or 0) for v in totals.values())
    if total < min_tokens:
        return ("low-volume",
                "%d priced token(s) in the window, too few to conclude anything"
                % total)

    us = int(totals.get("us") or 0)
    if us <= 0:
        if int(totals.get("not_available") or 0) >= total:
            return ("geo-unsupported",
                    "%.1fM priced token(s), all on models that predate the "
                    "inference_geo parameter. No premium and no lever."
                    % (total / 1e6))
        return ("no-us-traffic",
                "%.1fM priced token(s) and none of it on inference_geo us"
                % (total / 1e6))

    share = us / float(total)
    shape = ("%.0f%% of %.1fM priced token(s) on inference_geo us"
             % (share * 100, total / 1e6))

    if default_geo == "us":
        return ("us-by-workspace-default",
                "%s; data_residency.default_inference_geo is us, so every "
                "caller pays the 1.1x whether or not any of them asked."
                % shape)
    if default_geo == "global":
        return ("us-by-request",
                "%s while the workspace default is global, so callers are "
                "setting inference_geo explicitly. The fix is in code, not in "
                "the workspace." % shape)
    return ("us-unexplained",
            "%s with no readable data_residency default. Read the workspace "
            "before deciding whether this is deliberate." % shape)


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
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


def workspaces(session):
    """Every workspace, keyed by id, including archived ones."""
    out = {}
    params = {"limit": 100, "include_archived": "true"}
    while True:
        page = get(session, "/organizations/workspaces", params)
        for item in page.get("data") or []:
            out[str(item.get("id"))] = item
        if not page.get("has_more") or not page.get("last_id"):
            return out
        params = dict(params, after_id=page["last_id"])


def spend_by_workspace(session, start):
    """Thirty days of spend per workspace. amount is a decimal string."""
    out = {}
    for page in pages(session, "/organizations/cost_report",
                      {"starting_at": start, "limit": 31,
                       "group_by[]": ["workspace_id"]}):
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                workspace = str(result.get("workspace_id") or "default workspace")
                raw = result.get("amount")
                try:
                    out[workspace] = out.get(workspace, 0.0) + float(raw or 0.0)
                except (TypeError, ValueError):
                    pass
    return out


def window_start(days):
    """Floor to midnight UTC: starting_at must sit on a bucket boundary."""
    now = dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of daily buckets to read (default 30)")
    ap.add_argument("--min-tokens", type=int, default=1_000_000,
                    help="priced tokens below which no claim is made")
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
                       "group_by[]": ["inference_geo", "workspace_id"]}))
    directory = workspaces(s)
    spend = spend_by_workspace(s, start)

    checked = 0
    bad = 0
    for workspace in sorted(rows, key=lambda w: -(rows[w].get("us") or 0)):
        totals = rows[workspace]
        default_geo = residency_default(directory.get(workspace))
        state, detail = verdict(totals, default_geo, args.min_tokens)
        checked += 1
        line = "%-24s %-16s %s" % (state, workspace, detail)

        if state not in FINDINGS:
            log.info(line)
            continue
        bad += 1
        log.warning(line)
        billed = spend.get(workspace, 0.0)
        log.warning("  estimated premium about $%.2f of $%.2f spend in this "
                    "window, assuming a similar token mix across geos",
                    premium_estimate(billed, us_share(totals)), billed)
        allowed = ((directory.get(workspace) or {}).get("data_residency")
                   or {}).get("allowed_inference_geos")
        if allowed:
            log.warning("  allowed_inference_geos: %s", ", ".join(map(str, allowed)))
        if state == "us-by-workspace-default":
            log.warning("  repair: confirm which contract requires US residency, "
                        "and whether that traffic can live in its own workspace "
                        "instead of every workspace paying for it")
        elif state == "us-by-request":
            log.warning("  repair: the callers are setting inference_geo "
                        "themselves. Find them before changing anything here.")
        else:
            log.warning("  repair: read this workspace's data_residency block "
                        "and record why it is set the way it is")
        log.warning("  do not change residency from a script: it is a "
                    "compliance setting with a named owner")

    log.info("%d workspace(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
