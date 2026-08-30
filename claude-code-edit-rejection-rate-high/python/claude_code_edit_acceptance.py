"""Find Claude Code actors whose edit proposals are mostly rejected.

Read only. One paged GET per UTC day against the Claude Code usage report with
an Admin API key. No message is sent and nothing is written.

Every rejected proposal was fully generated and fully billed before it was
displayed, so this is the one audit in the set whose subject is output that
succeeded, cost full rates, and was then thrown away by the person it was
produced for.

The acceptance rate and the estimated cost are printed as two separate
readings and are never multiplied. The report carries no per-proposal token
counts, so there is no way to know whether the rejected diffs were the large
ones or the small ones, and "60% of the spend was wasted" would be a sentence
this API cannot support.

The API never says why a proposal was rejected. The repair is a conversation
with the team that owns the repository, so it is printed rather than performed.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude_code_edit_acceptance")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# The tools that propose a change a person then keeps or discards. Counted
# apart rather than averaged: a bad multi_edit rate usually means a task scoped
# too wide, and a bad write rate usually means whole files where an edit was
# wanted, and one number cannot say either.
EDIT_TOOLS = ("edit_tool", "multi_edit_tool", "write_tool", "notebook_edit_tool")

FINDINGS = ("rejected-more-than-kept", "low-acceptance")


def actor_name(record):
    """Who the record belongs to. Pure. Both actor shapes, plus neither."""
    actor = (record or {}).get("actor")
    if not isinstance(actor, dict):
        return "unattributed"
    for field in ("email_address", "api_key_name"):
        value = str(actor.get(field) or "").strip()
        if value:
            return value
    return "unattributed"


def mask(name):
    """Hide the local part of an email address. Pure. Non-emails pass through.

    This output attaches a quality number to a named person. Masked by default
    is the only sensible default for that.
    """
    text = str(name or "").strip()
    if "@" not in text:
        return text or "unattributed"
    local, _, domain = text.partition("@")
    if not local:
        return text
    return local[0] + "***@" + domain


def actions_of(record):
    """Accepted and rejected counts per edit tool on one record. Pure.

    Tools absent from the record are omitted rather than zeroed, because a tool
    nobody used and a tool used with nothing accepted must not look alike.
    """
    actions = (record or {}).get("tool_actions")
    actions = actions if isinstance(actions, dict) else {}
    out = {}
    for tool in EDIT_TOOLS:
        row = actions.get(tool)
        if not isinstance(row, dict):
            continue
        counts = {}
        for field in ("accepted", "rejected"):
            try:
                counts[field] = max(0, int(row.get(field) or 0))
            except (TypeError, ValueError):
                counts[field] = 0
        if counts["accepted"] or counts["rejected"]:
            out[tool] = counts
    return out


def fold(pages):
    """Fold every record into one row per actor. Pure.

    The productivity fields travel with the rate on purpose. A low acceptance
    rate beside twenty-six commits is a different conversation from a low rate
    beside none, and separating them invites the wrong one.
    """
    rows = {}
    for page in pages or []:
        for record in (page or {}).get("data") or []:
            if not isinstance(record, dict):
                continue
            who = actor_name(record)
            row = rows.setdefault(who, {
                "tools": {}, "days": 0, "sessions": 0, "commits": 0, "prs": 0,
                "added": 0, "removed": 0, "cents": 0.0})
            row["days"] += 1
            for tool, counts in actions_of(record).items():
                into = row["tools"].setdefault(tool, {"accepted": 0, "rejected": 0})
                into["accepted"] += counts["accepted"]
                into["rejected"] += counts["rejected"]

            core = record.get("core_metrics")
            core = core if isinstance(core, dict) else {}
            for field, key in (("num_sessions", "sessions"),
                               ("commits_by_claude_code", "commits"),
                               ("pull_requests_by_claude_code", "prs")):
                try:
                    row[key] += max(0, int(core.get(field) or 0))
                except (TypeError, ValueError):
                    pass
            lines = core.get("lines_of_code")
            lines = lines if isinstance(lines, dict) else {}
            for field, key in (("added", "added"), ("removed", "removed")):
                try:
                    row[key] += max(0, int(lines.get(field) or 0))
                except (TypeError, ValueError):
                    pass

            for entry in record.get("model_breakdown") or []:
                cost = (entry or {}).get("estimated_cost")
                cost = cost if isinstance(cost, dict) else {}
                try:
                    row["cents"] += float(cost.get("amount") or 0)
                except (TypeError, ValueError):
                    pass
    return rows


def totals(row):
    """Accepted and rejected across every edit tool for one actor. Pure."""
    accepted = 0
    rejected = 0
    for counts in ((row or {}).get("tools") or {}).values():
        accepted += max(0, int((counts or {}).get("accepted") or 0))
        rejected += max(0, int((counts or {}).get("rejected") or 0))
    return accepted, rejected


def acceptance(counts):
    """accepted / (accepted + rejected). Pure. None when nothing was proposed.

    None rather than 0.0. An actor who proposed nothing has no acceptance rate,
    and reporting one as zero would put them at the top of a list of the worst.
    """
    data = counts or {}
    accepted = max(0, int(data.get("accepted") or 0))
    rejected = max(0, int(data.get("rejected") or 0))
    total = accepted + rejected
    if total <= 0:
        return None
    return accepted / float(total)


def worst_tool(row, min_proposals=10):
    """The lowest-scoring tool with enough volume to mean it. Pure.

    Returns (tool, rate) or None. The per-tool floor is separate from and lower
    than the actor floor, because one tool is a slice of an actor's traffic.
    """
    worst = None
    for tool, counts in sorted(((row or {}).get("tools") or {}).items()):
        total = (max(0, int((counts or {}).get("accepted") or 0))
                 + max(0, int((counts or {}).get("rejected") or 0)))
        if total < min_proposals:
            continue
        rate = acceptance(counts)
        if rate is None:
            continue
        if worst is None or rate < worst[1]:
            worst = (tool, rate)
    return worst


def verdict(row, min_proposals=20, keep_floor=0.50, thin=0.70):
    """Classify one actor's acceptance. Pure. Returns (state, detail)."""
    accepted, rejected = totals(row)
    total = accepted + rejected
    if total < min_proposals:
        return ("too-few-proposals",
                "%d proposal(s), under the floor of %d: a bad afternoon is not "
                "a pattern" % (total, min_proposals))

    rate = acceptance({"accepted": accepted, "rejected": rejected})
    worst = worst_tool(row)
    tail = ""
    if worst is not None:
        tail = "; worst tool %s at %.0f%%" % (worst[0], worst[1] * 100)

    if rate < keep_floor:
        return ("rejected-more-than-kept",
                "%.0f%% accepted over %d proposal(s)%s: a majority of the "
                "diffs shown were discarded after being generated and billed"
                % (rate * 100, total, tail))
    if rate < thin:
        return ("low-acceptance",
                "%.0f%% accepted over %d proposal(s)%s"
                % (rate * 100, total, tail))
    return ("healthy",
            "%.0f%% accepted over %d proposal(s)%s" % (rate * 100, total, tail))


def repair_lines(state, row):
    """The repair for one classified actor. Pure. A conversation, not a change."""
    if state not in FINDINGS:
        return []
    commits = max(0, int((row or {}).get("commits") or 0))
    lines = [
        "review project setup for these repositories: a CLAUDE.md context "
        "file so the model knows where things live, and narrower task "
        "scoping so a proposal is small enough to be judged.",
        "check the model and effort level against the work. A frontier model "
        "on a mechanical edit produces confident, wide diffs that get "
        "rejected on scope rather than on correctness.",
    ]
    if commits > 0:
        lines.append("this actor still landed %d commit(s) in the window, so "
                     "the tool is producing accepted work as well. Read the "
                     "rate as a cost per accepted change, not as a failure."
                     % commits)
    else:
        lines.append("no commits landed through Claude Code in the window, so "
                     "there is no accepted work to weigh the rejections "
                     "against.")
    return lines


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params):
    """Walk one day of the paginated Claude Code usage report."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        yield page
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def day_strings(days, today=None):
    """The UTC dates to request, newest first. Pure. Today is excluded."""
    end = today or dt.datetime.now(dt.timezone.utc).date()
    return [(end - dt.timedelta(days=n)).isoformat()
            for n in range(1, max(1, int(days)) + 1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14,
                    help="UTC days to read, ending yesterday (default 14)")
    ap.add_argument("--min-proposals", type=int, default=20,
                    help="proposals below which no claim is made (default 20)")
    ap.add_argument("--floor", type=float, default=0.70,
                    help="acceptance below which a rate is called low")
    ap.add_argument("--show-actors", action="store_true",
                    help="print email addresses in full instead of masked")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    collected = []
    days = day_strings(args.days)
    for day in days:
        collected.extend(pages(s, "/organizations/usage_report/claude_code",
                               {"starting_at": day, "limit": 1000}))

    rows = fold(collected)
    if not rows:
        log.info("no Claude Code records over %d day(s). This report covers "
                 "Claude Code on the Claude API only.", len(days))
        return 0

    bad = 0
    for who in sorted(rows, key=lambda a: -rows[a]["cents"]):
        row = rows[who]
        state, detail = verdict(row, args.min_proposals, thin=args.floor)
        label = who if args.show_actors else mask(who)
        line = "%-24s %-20s %s, $%.2f" % (state, label, detail,
                                          row["cents"] / 100.0)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
            for repair in repair_lines(state, row):
                log.warning("  repair: %s", repair)
        else:
            log.info(line)

    log.info("%d actor(s) over %d day(s), %d finding(s)",
             len(rows), len(days), bad)
    log.info("the rate and the cost are separate readings: no per-proposal "
             "token counts exist to join them, so the share of spend that was "
             "discarded is not a number this API can support")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
