"""Find an OpenAI organization with no project boundary to enforce anything on.

Read only. Two paged GETs against /v1/organization/projects and
/v1/organization/costs, plus one per dominant project for its key NAMES. Every
request is a GET and no request body is ever built.

The finding is the absence of a boundary, not the concentration of spend. A
single active project holds 100% of cost by construction, which is arithmetic;
a dominant project in an organization that has nine is a different reading with
a different repair, and this script names that reading rather than claiming it.

Key values are never read or printed. The key listing is used for the `name`
field only, and only as corroboration.
"""
import argparse
import datetime as dt
import logging
import os
import re
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_project_boundary_audit")

API = "https://api.openai.com/v1"
UNGROUPED = "ungrouped"

# Environment words, matched as WHOLE tokens after splitting on anything that is
# not a letter or a digit. Substring matching here is how "devops-runner" gets
# reported as a development key and "provider-proxy" as a production one, which
# is a false positive attached to a person's naming habits.
ENV_WORDS = {
    "prod": "prod", "production": "prod", "live": "prod",
    "stage": "staging", "staging": "staging", "preprod": "staging",
    "dev": "dev", "development": "dev",
    "local": "local", "laptop": "local",
    "test": "test", "testing": "test", "qa": "test",
    "ci": "ci", "build": "ci",
    "sandbox": "sandbox", "scratch": "sandbox", "playground": "sandbox",
}

FINDINGS = ("no-boundary", "boundary-unused")


def active(projects):
    """Projects that can still receive traffic. Pure.

    Archived projects are dropped on either signal. `status` is the documented
    field and `archived_at` is the one that is reliably present, and a listing
    that carries one without the other is common enough that trusting a single
    field over-counts the boundary.
    """
    out = []
    for project in projects or []:
        row = project or {}
        if str(row.get("status") or "").strip().lower() == "archived":
            continue
        if row.get("archived_at"):
            continue
        out.append(row)
    return out


def spend_by_project(buckets):
    """{project_id: dollars} from the cost report. Pure.

    A result with a null project_id is an UNGROUPED row, not a project. Folding
    those into the ranking is how a forgotten group_by turns into a confident
    report of one enormous project in an organization that has twelve.
    """
    rows = {}
    for bucket in buckets or []:
        for result in (bucket or {}).get("results") or []:
            row = result or {}
            name = row.get("project_id") or UNGROUPED
            try:
                value = float((row.get("amount") or {}).get("value") or 0.0)
            except (TypeError, ValueError):
                continue
            rows[str(name)] = rows.get(str(name), 0.0) + value
    return rows


def shares(spend):
    """[(project_id, dollars, share)] over real projects only. Pure.

    Sorted by dollars descending. UNGROUPED is excluded from both the ranking
    and the denominator, so a share is always a share of attributable spend.
    """
    rows = {k: v for k, v in (spend or {}).items() if k != UNGROUPED}
    total = sum(rows.values())
    out = [(k, round(v, 2), (v / total) if total > 0 else 0.0)
           for k, v in rows.items()]
    out.sort(key=lambda r: (-r[1], r[0]))
    return out


def environments(name):
    """The environment classes named in one identifier. Pure.

    Tokenised on non-alphanumerics and matched whole, so "devops" is a team and
    "provider" is a noun. Returns a set, possibly empty.
    """
    tokens = re.split(r"[^a-z0-9]+", str(name or "").strip().lower())
    return {ENV_WORDS[t] for t in tokens if t in ENV_WORDS}


def mixed(names):
    """Every environment class named across a set of identifiers. Pure."""
    found = set()
    for name in names or []:
        found |= environments(name)
    return found


def verdict(active_count, ranked, min_spend=1.0, dominant=0.95):
    """Classify the organization's topology. Pure. Returns (state, detail).

    The container count is read before any money, because a share of total is a
    comparison and a single-project organization has nothing to compare with.
    """
    rows = list(ranked or [])
    total = round(sum(row[1] for row in rows), 2)

    if active_count <= 0:
        return ("no-active-projects",
                "the listing returned no active project at all, which usually "
                "means the key could not see them rather than that none exist")
    if active_count == 1:
        return ("no-boundary",
                "1 active project holds 100%% of $%s. There is no second "
                "container to cap, alert on, rate limit or attribute against."
                % format(total, ",.2f"))
    if total < min_spend:
        return ("no-spend-yet",
                "%d active project(s) and $%s of attributable spend in the "
                "window. The boundary exists and nothing has tested it yet."
                % (active_count, format(total, ",.2f")))

    top_id, top_amount, top_share = rows[0]
    quiet = [row for row in rows[1:] if row[1] <= 0.0]
    if top_share >= dominant and len(quiet) == len(rows) - 1:
        return ("boundary-unused",
                "%d active project(s), and %s carries %.0f%% of $%s while every "
                "other project has no spend at all. The containers exist and no "
                "traffic routes to them, so the controls on them enforce nothing."
                % (active_count, top_id, top_share * 100, format(total, ",.2f")))
    if top_share >= dominant:
        return ("concentration-not-topology",
                "%d active project(s), and %s carries %.0f%% of $%s. This "
                "organization has a boundary, so that is a concentration "
                "reading rather than a topology one and has a different repair."
                % (active_count, top_id, top_share * 100, format(total, ",.2f")))
    return ("separated",
            "%d active project(s) sharing $%s, top project at %.0f%%"
            % (active_count, format(total, ",.2f"), top_share * 100))


def repair_lines(state, envs=()):
    """The repair for one topology verdict. Pure. Printed, never performed."""
    found = sorted(envs or ())
    if state == "no-boundary":
        lines = [
            "create prod, staging and dev with POST /v1/organization/projects, "
            "which is the smallest split that lets any control differ.",
            "give each project its own service account and key, then move "
            "traffic one key at a time rather than in one cutover.",
            "spend limits, spend alerts, rate limits, model permissions and "
            "data retention are all configured per project and cannot differ "
            "until the projects do.",
            "projects can be archived but never deleted, so the names are "
            "permanent. Spend ten minutes on them once.",
        ]
        if found:
            lines.insert(0, "the environments already exist in your key names "
                            "(%s); they are simply not represented in the "
                            "platform." % ", ".join(found))
        return lines
    if state == "boundary-unused":
        return [
            "the projects are not the problem. Nothing routes to them.",
            "issue a key in the quiet projects and move the traffic that "
            "belongs there, then set the limits per project afterwards.",
            "until traffic actually lands in a project, every control "
            "configured on it is inert.",
        ]
    if state == "concentration-not-topology":
        return [
            "do not restructure on this reading. Rank the cost rows by share "
            "of total and ask which line item is expensive instead.",
        ]
    return []


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an "
                         "organization admin key, not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def paged(session, path, **params):
    """Walk an after/last_id cursor listing."""
    params = dict(params)
    while True:
        page = get(session, path, **params)
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params["after"] = page.get("last_id") or (data[-1] or {}).get("id")


def cost_buckets(session, params, max_pages=40):
    """Walk the paged cost report."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, "/organization/costs", **params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def window_start(days, now=None):
    """Unix seconds at midnight UTC, `days` ago."""
    now = now or dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - dt.timedelta(days=days)).timestamp())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of cost to read (default 30)")
    ap.add_argument("--dominant", type=float, default=0.95,
                    help="share above which one project is called dominant")
    ap.add_argument("--no-key-names", action="store_true",
                    help="skip the key-name corroboration read")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key; a "
                  "project key cannot read /v1/organization/*")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})

    projects = list(paged(s, "/organization/projects", limit=100,
                          include_archived="true"))
    live = active(projects)
    spend = spend_by_project(cost_buckets(
        s, {"start_time": window_start(args.days), "bucket_width": "1d",
            "limit": min(args.days, 30), "group_by": "project_id"}))
    ranked = shares(spend)
    total = round(sum(row[1] for row in ranked), 2)

    log.info("%d active project(s), %d archived, $%s in the last %d day(s)",
             len(live), len(projects) - len(live), format(total, ",.2f"),
             args.days)
    if spend.get(UNGROUPED):
        log.info("$%s of cost came back ungrouped and is not counted as a "
                 "project", format(spend[UNGROUPED], ",.2f"))

    envs = set()
    if not args.no_key_names and live:
        target = live[0]
        if ranked:
            by_id = {p.get("id"): p for p in live}
            target = by_id.get(ranked[0][0], target)
        names = [(k or {}).get("name") or ""
                 for k in paged(s, "/organization/projects/%s/api_keys"
                                % target.get("id"), limit=100,
                                owner_project_access="any")]
        envs = mixed(names)
        if envs:
            log.info("key names in %s already name %d environment(s): %s",
                     target.get("name") or target.get("id"), len(envs),
                     ", ".join(sorted(envs)))

    state, detail = verdict(len(live), ranked, dominant=args.dominant)
    if state in FINDINGS:
        log.warning("%-26s %s", state, detail)
        for line in repair_lines(state, envs):
            log.warning("  repair: %s", line)
        log.info("1 finding(s)")
        return 1

    log.info("%-26s %s", state, detail)
    for line in repair_lines(state, envs):
        log.info("  note: %s", line)
    log.info("0 finding(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
