"""Report OpenAI projects whose configured service tier and invoice disagree.

Read only. Two GET requests against the organization endpoints and nothing
else. Those endpoints reject project keys, so this needs an organization admin
key (sk-admin-), which can and should be provisioned read-only.

The finding is a mismatch rather than a total. A project set to the premium tier
whose spend lands on standard line items is being downgraded and is not getting
what it configured; a project set to standard carrying premium line items has a
code path sending the parameter. Both are printed with the repair, and neither
repair is performed here.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_fast_mode_tier_audit")

API = "https://api.openai.com/v1"

# Fast mode is priced at twice the standard rate. The multiplier is here to
# describe the finding, not to price your traffic: the dollars come from the
# cost report, which does not go stale the way a typed-in price table does.
PREMIUM_MULTIPLIER = 2.0

# line_item is a human-readable label, not a documented enum, so premium traffic
# is matched by substring and every matched string is printed for you to check.
PREMIUM_WORDS = ("fast", "priority")

# What the project object calls the setting the console calls Project Service
# Tier. Read leniently and in this order; absent is reported as absent.
TIER_FIELDS = ("service_tier", "default_service_tier")

FINDINGS = ("downgraded", "partly-downgraded", "unrequested-premium")


def tier_of(project):
    """Read a project's configured service tier. Pure.

    Returns a lowercase string, or None when the object carries no such field.
    None is not "standard": a missing field means this script cannot see the
    setting, and reporting that as a configured default would turn every
    unreadable project into a false clean.
    """
    candidates = []
    for field in TIER_FIELDS:
        candidates.append(project.get(field))
    settings = project.get("settings")
    if isinstance(settings, dict):
        for field in TIER_FIELDS:
            candidates.append(settings.get(field))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def split_spend(buckets, project_id):
    """Split one project's spend into premium and standard dollars. Pure.

    Returns (premium, standard, labels) where labels are the distinct line_item
    strings that matched as premium. The strings come back so the report can
    show what it matched on rather than asking you to trust a substring test.
    """
    premium = 0.0
    standard = 0.0
    labels = set()
    for bucket in buckets or []:
        for result in bucket.get("results") or []:
            if str(result.get("project_id") or "") != str(project_id):
                continue
            label = str(result.get("line_item") or "")
            try:
                value = float((result.get("amount") or {}).get("value") or 0.0)
            except (TypeError, ValueError):
                continue
            low = label.lower()
            if any(word in low for word in PREMIUM_WORDS):
                premium += value
                if value:
                    labels.add(label)
            else:
                standard += value
    return (round(premium, 2), round(standard, 2), sorted(labels))


def overrides(pairs):
    """Parse --tier project_id=tier arguments into a dict. Pure.

    For organizations whose project objects do not carry the setting at all: you
    read it once in the console and hand it to the script, rather than the
    script guessing.
    """
    out = {}
    for pair in pairs or []:
        if "=" not in str(pair):
            continue
        name, _, value = str(pair).partition("=")
        name, value = name.strip(), value.strip().lower()
        if name and value:
            out[name] = value
    return out


def verdict(tier, premium, standard, min_spend=1.0, delivered=0.60):
    """Classify one project. Pure. Returns (state, detail).

    The two findings are opposite and are never collapsed. "downgraded" costs
    latency you thought you had bought; "unrequested-premium" costs money nobody
    budgeted. A script that printed "tier mismatch" for both would leave the
    reader to work out which of those they were looking at.
    """
    premium = max(0.0, float(premium or 0.0))
    standard = max(0.0, float(standard or 0.0))
    total = premium + standard
    tier = (tier or "").strip().lower() or None

    if total < min_spend:
        return ("no-spend",
                "$%.2f of spend in the window, too little to say anything about "
                "which tier served it" % total)

    share = premium / total

    if tier in ("fast", "priority"):
        if premium <= 0:
            return ("downgraded",
                    "configured for the %s tier and not one dollar of $%.2f in "
                    "spend is on a premium line item. Every request in the "
                    "window was served on the default tier."
                    % (tier, total))
        if share < delivered:
            return ("partly-downgraded",
                    "configured for the %s tier, and only %.0f%% of $%.2f in "
                    "spend is on premium line items. The rest was downgraded "
                    "and served at default latency." % (tier, share * 100, total))
        return ("premium-delivered",
                "configured for the %s tier and %.0f%% of $%.2f is billed at it. "
                "The premium is being delivered and charged at about %.1fx the "
                "standard rate, so somebody should still want it."
                % (tier, share * 100, total, PREMIUM_MULTIPLIER))

    if tier is None:
        if premium > 0:
            return ("unknown-tier-premium",
                    "the project object carries no readable service tier and "
                    "$%.2f of $%.2f is on premium line items. Read the setting "
                    "in the console and pass it with --tier."
                    % (premium, total))
        return ("unknown-tier",
                "the project object carries no readable service tier. No "
                "premium line items in $%.2f of spend, so nothing is being "
                "billed at the premium rate today." % total)

    if premium > 0:
        return ("unrequested-premium",
                "the project tier is %s and %.0f%% of $%.2f is on premium line "
                "items, so a code path is sending the tier in the request body. "
                "That traffic bills at about %.1fx the standard rate."
                % (tier, share * 100, total, PREMIUM_MULTIPLIER))
    return ("standard",
            "tier is %s and no premium line items in $%.2f of spend" % (tier, total))


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an "
                         "organization admin key (sk-admin-), not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def projects(session, page_size, max_pages):
    """Walk GET /v1/organization/projects, which paginates on the last id."""
    params = {"limit": page_size}
    for _ in range(max_pages):
        page = get(session, "/organization/projects", params)
        data = page.get("data") or []
        for project in data:
            yield project
        if not page.get("has_more") or not data:
            return
        params = {"limit": page_size, "after": data[-1].get("id")}


def cost_pages(session, params, max_pages=40):
    """Walk the cost report, which paginates on an opaque page cursor."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, "/organization/costs", params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params = dict(params)
        params["page"] = page["next_page"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of daily cost buckets to read (default 30)")
    ap.add_argument("--min-spend", type=float, default=1.0,
                    help="ignore projects below this many dollars (default 1.0)")
    ap.add_argument("--delivered", type=float, default=0.60,
                    help="premium share above which the tier counts as "
                         "delivered (default 0.60)")
    ap.add_argument("--tier", action="append", default=[], metavar="ID=TIER",
                    help="supply a project's configured tier when the object "
                         "does not carry it, e.g. --tier proj_abc=fast")
    ap.add_argument("--show-all", action="store_true",
                    help="also print projects whose tier and invoice agree")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_ADMIN_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key, read-only "
                  "scopes are enough)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    told = overrides(args.tier)
    costs = list(cost_pages(session, {
        "start_time": int(time.time()) - args.days * 86400,
        "bucket_width": "1d",
        "limit": min(180, max(1, args.days)),
        "group_by": ["line_item", "project_id"],
    }))

    checked = 0
    found = 0
    for project in projects(session, 100, 20):
        project_id = str(project.get("id") or "")
        name = str(project.get("name") or project_id)
        tier = told.get(project_id) or tier_of(project)
        premium, standard, labels = split_spend(costs, project_id)
        state, detail = verdict(tier, premium, standard, args.min_spend,
                                args.delivered)
        checked += 1
        line = "%-21s %s (%s)  %s" % (state, project_id, name, detail)

        if state in FINDINGS:
            found += 1
            log.warning(line)
            if labels:
                log.warning("  matched premium line item(s): %s",
                            ", ".join(labels))
            if state == "unrequested-premium":
                log.warning("  repair: find the call site sending the tier in "
                            "the request body and drop it, or budget for it "
                            "deliberately. Nothing in the project settings asked "
                            "for this.")
            else:
                log.warning("  repair: either stop paying for a tier you are not "
                            "being served (set Project Service Tier back to "
                            "standard) or ask OpenAI to raise the ramp limits "
                            "that are downgrading you. Decide which, then log "
                            "the response envelope's service_tier so the "
                            "downgrade rate is a metric instead of an audit.")
        elif state in ("unknown-tier", "unknown-tier-premium"):
            log.warning(line)
        elif args.show_all:
            log.info(line)

    log.info("%d project(s) checked, %d with a tier the invoice disagrees with",
             checked, found)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
