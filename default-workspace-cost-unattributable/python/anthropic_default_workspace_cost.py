"""Find Anthropic cost that reports no workspace, and the keys behind it.

Read only. Three paged GETs against /v1/organizations/* with an Admin API key.
Nothing is sent to /v1/messages and no request body is constructed.

The unallocated bucket has two causes and only one of them has a repair. Cost
and usage in the organization's default workspace report workspace_id: null,
and Console playground usage reports api_key_id: null because no key was
involved. Keys can be moved; playground traffic cannot, so the script sizes
both before it recommends anything.

Key values are never read or printed. The key listing is used for ids, names
and scope only.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_default_workspace_cost")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# The default workspace has no id to report, so the reports return null for it.
# Kept under an explicit sentinel rather than dropped, because dropping it is
# how a chargeback report silently stops adding up to the invoice.
DEFAULT_WS = "(default workspace)"

PLAYGROUND = "console-playground"
DEFAULT_KEYED = "default-workspace"
ATTRIBUTED = "attributed"

ORG_SCOPED = "organization-scoped"
NAMED = "named-workspace"
UNKNOWN_SCOPE = "unknown-scope"

MOVABLE = (ORG_SCOPED, DEFAULT_KEYED)
FINDINGS = ("movable-keys", "console-playground", "unattributable-no-key-to-move")


def amount(row):
    """One cost row's amount as a float. Pure.

    The cost report returns amount as a decimal STRING. Summing the raw values
    concatenates them, which produces a number large enough that nobody reads
    it as money and small enough that nobody notices it is text.
    """
    try:
        return float((row or {}).get("amount") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cost_by_workspace(pages):
    """{workspace_id: dollars} from the cost report. Pure. Null uses a sentinel."""
    rows = {}
    for page in pages or []:
        for bucket in (page or {}).get("data") or []:
            for result in (bucket or {}).get("results") or []:
                key = (result or {}).get("workspace_id") or DEFAULT_WS
                rows[str(key)] = rows.get(str(key), 0.0) + amount(result)
    return rows


def unattributed_share(rows):
    """The null workspace's share of total cost. Pure. 0.0 when empty."""
    data = rows or {}
    total = sum(data.values())
    if total <= 0:
        return 0.0
    return float(data.get(DEFAULT_WS, 0.0)) / total


def weigh(result):
    """Total billed tokens on one usage row. Pure.

    cache_creation is an object rather than a scalar, so a reader that treats
    it as an int drops every cached write out of the weight.
    """
    row = result or {}
    total = 0
    for field in ("uncached_input_tokens", "cache_read_input_tokens",
                  "output_tokens"):
        try:
            total += int(row.get(field) or 0)
        except (TypeError, ValueError):
            pass
    creation = row.get("cache_creation")
    if isinstance(creation, dict):
        for value in creation.values():
            try:
                total += int(value or 0)
            except (TypeError, ValueError):
                pass
    return total


def usage_split(pages):
    """{cause: tokens} across the usage report. Pure.

    A null api_key_id is classified BEFORE a null workspace_id. A playground
    request made in the default workspace has both fields null, and counting it
    in both buckets inflates the movable half of the finding, which is the half
    the script is about to recommend work on.
    """
    out = {PLAYGROUND: 0, DEFAULT_KEYED: 0, ATTRIBUTED: 0}
    for page in pages or []:
        for bucket in (page or {}).get("data") or []:
            for result in (bucket or {}).get("results") or []:
                row = result or {}
                tokens = weigh(row)
                if not row.get("api_key_id"):
                    out[PLAYGROUND] += tokens
                elif not row.get("workspace_id"):
                    out[DEFAULT_KEYED] += tokens
                else:
                    out[ATTRIBUTED] += tokens
    return out


def playground_share(split):
    """Playground share of the null bucket only. Pure. 0.0 when the bucket is empty."""
    data = split or {}
    null_bucket = int(data.get(PLAYGROUND, 0)) + int(data.get(DEFAULT_KEYED, 0))
    if null_bucket <= 0:
        return 0.0
    return int(data.get(PLAYGROUND, 0)) / float(null_bucket)


def key_attribution(key):
    """Where one API key's traffic lands. Pure. Returns (kind, workspace_id).

    scope.workspace_id is read before the deprecated top-level workspace_id,
    which is null for keys bound to the default workspace. An unrecognised
    scope type is returned as unknown rather than assumed harmless.
    """
    row = key or {}
    scope = row.get("scope") or {}
    kind = str(scope.get("type") or "").strip().lower()
    workspace = scope.get("workspace_id") or row.get("workspace_id")

    if kind == "organization":
        return (ORG_SCOPED, None)
    if kind and kind != "workspace":
        return (UNKNOWN_SCOPE, workspace and str(workspace) or None)
    if workspace:
        return (NAMED, str(workspace))
    return (DEFAULT_KEYED, None)


def fold_keys(keys):
    """{kind: [{id, name, workspace_id}]} over ACTIVE keys only. Pure.

    An inactive key cannot be the cause of spend in the window and must not
    appear in a migration list somebody is going to work through by hand.
    """
    out = {ORG_SCOPED: [], DEFAULT_KEYED: [], NAMED: [], UNKNOWN_SCOPE: []}
    for key in keys or []:
        row = key or {}
        if str(row.get("status") or "active").strip().lower() != "active":
            continue
        kind, workspace = key_attribution(row)
        out[kind].append({"id": str(row.get("id") or "unknown"),
                          "name": str(row.get("name") or "unnamed"),
                          "workspace_id": workspace})
    return out


def verdict(share, total, folded, split, min_spend=1.0, min_share=0.10,
            playground_max=0.50):
    """Classify the unallocated bucket. Pure. Returns (state, detail)."""
    movable = sum(len(folded.get(kind) or []) for kind in MOVABLE)
    if total < min_spend:
        return ("no-spend-yet",
                "$%s of cost in the window, too little to conclude anything"
                % format(total, ",.2f"))
    if share < min_share:
        return ("attributed",
                "%.0f%% of $%s has a null workspace_id, under the threshold"
                % (share * 100, format(total, ",.2f")))

    plays = playground_share(split)
    if plays > playground_max:
        return ("console-playground",
                "%.0f%% of $%s has no workspace on it, and %.0f%% of that usage "
                "carries no api_key_id either. That is Console playground "
                "traffic, and no key can be moved to make it land anywhere."
                % (share * 100, format(total, ",.2f"), plays * 100))
    if movable:
        return ("movable-keys",
                "%.0f%% of $%s has no workspace on it, and %d active key(s) "
                "land in the default workspace or carry organization scope."
                % (share * 100, format(total, ",.2f"), movable))
    return ("unattributable-no-key-to-move",
            "%.0f%% of $%s has no workspace on it, and every active key "
            "resolves to a named workspace. The spend came from a key that has "
            "since been deleted, or from the playground."
            % (share * 100, format(total, ",.2f")))


def repair_lines(state, folded, split):
    """The repair for one verdict. Pure. Printed, never performed."""
    plays = playground_share(split)
    if state == "movable-keys":
        lines = ["recreate each key inside a named workspace and cut over, key "
                 "by key. A key's workspace is fixed when it is created."]
        if folded.get(ORG_SCOPED):
            lines.append("%d of them carry organization scope, which is not a "
                         "workspace at all: those cannot be reassigned, only "
                         "replaced." % len(folded[ORG_SCOPED]))
        if plays > 0:
            lines.append("%.0f%% of the null usage is Console playground and no "
                         "key move touches it." % (plays * 100))
        lines.append("the default workspace cannot carry a rate-limit override "
                     "at all, so this traffic is also unbounded relative to the "
                     "organization limit.")
        return lines
    if state == "console-playground":
        return [
            "there is no key migration here. The requests carried no key.",
            "decide where experiments should run: a named workspace with its "
            "own key, or an accepted line in the chargeback report.",
            "the default workspace cannot carry a rate-limit override, so "
            "playground traffic competes with production for the org limit.",
        ]
    if state == "unattributable-no-key-to-move":
        return [
            "do not open a migration ticket. Every active key already resolves "
            "to a named workspace.",
            "the spend predates a key deletion or came from the playground; "
            "narrow the window and read the daily buckets to see which.",
        ]
    return []


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def report_pages(session, path, params):
    """Walk a usage or cost report on next_page."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        yield page
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def listing(session, path, params):
    """Walk an Admin list endpoint on after_id."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        for item in page.get("data") or []:
            yield item
        if not page.get("has_more") or not page.get("last_id"):
            return
        params["after_id"] = page["last_id"]


def window_start(days, now=None):
    """Floor to midnight UTC: starting_at must sit on a bucket boundary."""
    now = now or dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of daily buckets to read (default 30)")
    ap.add_argument("--min-share", type=float, default=0.10,
                    help="null share below which nothing is reported")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})
    start = window_start(args.days)

    costs = cost_by_workspace(report_pages(
        s, "/organizations/cost_report",
        {"starting_at": start, "limit": min(args.days + 1, 31),
         "group_by[]": ["workspace_id"]}))
    total = round(sum(costs.values()), 2)
    share = unattributed_share(costs)

    split = usage_split(report_pages(
        s, "/organizations/usage_report/messages",
        {"starting_at": start, "bucket_width": "1d",
         "limit": min(args.days + 1, 31),
         "group_by[]": ["api_key_id", "workspace_id"]}))

    folded = fold_keys(listing(s, "/organizations/api_keys", {"limit": 100}))

    log.info("$%s in the last %d day(s) across %d workspace row(s)",
             format(total, ",.2f"), args.days, len(costs))
    log.info("unattributed: $%s (%.0f%% of spend) has a null workspace_id",
             format(costs.get(DEFAULT_WS, 0.0), ",.2f"), share * 100)
    plays = playground_share(split)
    log.info("usage split of the null bucket: %.0f%% from API keys, %.0f%% "
             "Console playground", (1 - plays) * 100, plays * 100)

    state, detail = verdict(share, total, folded, split, min_share=args.min_share)
    if state not in FINDINGS:
        log.info("%-18s %s", state, detail)
        return 0

    log.warning("%-18s %s", state, detail)
    for kind in MOVABLE:
        for key in folded.get(kind) or []:
            log.warning("  %-12s %-22s %s", key["id"], key["name"], kind)
    for line in repair_lines(state, folded, split):
        log.warning("  repair: %s", line)
    log.info("1 finding(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
