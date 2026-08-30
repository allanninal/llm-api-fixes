"""Audit three closing surfaces for what can still be exported, and by whom.

Read only. Every request is a GET: the evals listing, one probe of the prompts
path, and one probe per prompt id you declare. Nothing here creates an eval, a
run or a prompt version.

The unit is exportability rather than validity, because what closes on
2026-11-30 is content held on the provider's side. The three surfaces are not
equally reachable and the script refuses to hide that: evals list cleanly,
reusable prompts have no documented list endpoint so the path is probed rather
than assumed, and Agent Builder has no REST surface at all and is graded
without a request.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sunset_export_audit")

API = "https://api.openai.com/v1"

# Announced 3 June 2026. Reusable Prompts, the Evals dashboard and API, and
# Agent Builder all close on this date. Published, not readable.
SHUTDOWN = "2026-11-30"

# Checked against the API reference index: there is a documented listing for
# evals and none for reusable prompts, and Agent Builder has no REST endpoints
# at all. That asymmetry is the reason this script grades reach per surface
# instead of running one loop over three paths.
AGENT_BUILDER = "agent-builder"

FINDINGS = ("no-api-surface", "no-list-endpoint", "not-readable",
            "not-a-prompt-id", "malformed", "credentials", "refused",
            "unreachable", "content-to-export")

REPAIRS = {
    "no-api-surface":
        "there is no endpoint, so nothing here automates it. Somebody has to "
        "open Agent Builder, export each published workflow, and rebuild it "
        "with the Agents SDK before the date.",
    "no-list-endpoint":
        "the API reference documents no listing for reusable prompts, so the "
        "authoritative roster is a grep of your own tree for pmpt_ ids. "
        "Anything only a colleague remembers comes out of the dashboard.",
    "not-readable":
        "nothing answered for this id, so its text is not retrievable by "
        "script. Copy it out of the dashboard and put it in the repository "
        "before the date, because after it there is nowhere to copy from.",
    "not-a-prompt-id":
        "reusable prompt ids start pmpt_. Fix the configuration; this one was "
        "never going to resolve, shutdown or no shutdown.",
    "content-to-export":
        "the listing carries the full definition, so one paginated GET is the "
        "whole export. Save it into the repository, then migrate the suites "
        "to Promptfoo.",
}


def days_left(today, when=SHUTDOWN):
    """Whole days from today to the date. Pure. Negative once it has passed."""
    return (dt.date.fromisoformat(str(when))
            - dt.date.fromisoformat(str(today))).days


def surface_reach(name, status):
    """How far the API gets on one surface. Pure. Returns (state, detail).

    Agent Builder is graded without a request and cannot be promoted by one:
    passing a 200 here still returns no-api-surface, because there is no path
    that 200 could have come from. That is asserted in a test, so a stray
    status from somewhere else can never make the report look complete.
    """
    if str(name) == AGENT_BUILDER:
        return ("no-api-surface",
                "no documented REST endpoints exist, so nothing here can "
                "inventory or export it")
    if status is None:
        return ("unreachable", "no response at all from this path")
    status = int(status)
    if status == 200:
        return ("enumerable",
                "the listing answered, so these can be exported by script")
    if status == 404:
        return ("no-list-endpoint",
                "nothing answered at this path, so ids have to come from your "
                "own call sites")
    if status in (401, 403):
        return ("credentials",
                "%d, so the reach of this surface was not established" % status)
    return ("refused", "%d, so the reach of this surface is unknown" % status)


def prompt_id_state(pid, status):
    """Grade one declared prompt id. Pure. Returns (state, detail).

    Shape first, response second. An id that is not a prompt id is a bug in the
    configuration and needs no request to prove it, and probing it anyway would
    bury a definite finding underneath a status code.
    """
    if not isinstance(pid, str) or not pid.strip():
        return ("malformed",
                "not a usable string, so this is a configuration bug rather "
                "than an id")
    pid = pid.strip()
    if not pid.startswith("pmpt_"):
        return ("not-a-prompt-id",
                "reusable prompt ids start pmpt_, so this is something else")
    if status is None:
        return ("not-probed", "no request was made for this id")
    status = int(status)
    if status == 200:
        return ("readable", "the stored content came back")
    if status == 404:
        return ("not-readable",
                "nothing answered, so its text comes out of the dashboard "
                "before the date")
    if status in (401, 403):
        return ("credentials", "%d, which is the key and not the id" % status)
    return ("refused", "%d" % status)


def export_plan(rows):
    """Turn reach into an owner per surface. Pure. [(name, owner, line)].

    The output somebody can actually act on: three rows, three owners, and no
    surface silently missing from the report.
    """
    plan = []
    for name, state in rows or []:
        if state == "enumerable":
            plan.append((name, "a script",
                         "one GET per page dumps the full objects"))
        elif state == "no-list-endpoint":
            plan.append((name, "a script, by id",
                         "probe the ids you hold; the rest is the dashboard"))
        elif state == "no-api-surface":
            plan.append((name, "a person",
                         "there is no endpoint, so nothing automates this"))
        else:
            plan.append((name, "a person, until proven otherwise",
                         "the reach could not be established, so assume the "
                         "dashboard"))
    return plan


def export_command(kind, ident=None):
    """The exact GET to run for one export. Pure. Printed, never performed."""
    auth = '-H "Authorization: Bearer $OPENAI_API_KEY"'
    if kind == "evals":
        return ("curl -s %s %s/evals?limit=100 > export/evals.json"
                % (auth, API))
    if kind == "prompt":
        return ("curl -s %s %s/prompts/%s > export/%s.json"
                % (auth, API, ident, ident))
    return ""


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed."""
    line = REPAIRS.get(state)
    if not line:
        return []
    if state in ("no-list-endpoint", "not-readable"):
        return [line,
                "then inline it: prompt={id: pmpt_...} becomes an instructions "
                "string you hold, which is the short half of this job and the "
                "half that is impossible before the export."]
    return [line]


def get_json(session, path, key, params=None, timeout=30):
    """One GET. Returns (status, parsed body). Never raises on a 4xx."""
    try:
        r = session.get(API + path,
                        headers={"Authorization": "Bearer " + key},
                        params=params or {}, timeout=timeout)
    except requests.RequestException as exc:
        log.debug("GET %s failed: %s", path, exc)
        return (None, {})
    try:
        return (r.status_code, r.json())
    except ValueError:
        return (r.status_code, {})


def all_evals(session, key, pages=50):
    """Walk GET /v1/evals to the end. Returns (status, [eval objects]).

    The listing carries data_source_config and testing_criteria, so the page is
    the export and there is no per-id fetch to write.
    """
    out, after, first = [], None, None
    for _ in range(pages):
        params = {"limit": 100, "order": "asc"}
        if after:
            params["after"] = after
        status, body = get_json(session, "/evals", key, params)
        if first is None:
            first = status
        if status != 200:
            break
        page = body.get("data") or []
        out.extend(page)
        if not page or not body.get("has_more"):
            break
        after = page[-1].get("id")
        if not after:
            break
    return (first, out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompt-id", action="append", default=[],
                    help="a pmpt_ id your code passes (repeatable)")
    ap.add_argument("--today", default=dt.date.today().isoformat(),
                    help="override the date the arithmetic is done against")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project read key. This script only "
                  "issues GET requests")
        return 2

    left = days_left(args.today)
    log.info("three surfaces close %s, %d day(s) %s", SHUTDOWN, abs(left),
             "left" if left >= 0 else "past")

    session = requests.Session()
    findings = 0
    reach = []

    eval_status, evals = all_evals(session, key)
    prompt_status, _ = get_json(session, "/prompts", key, {"limit": 1})
    probes = [("evals", eval_status), ("prompts", prompt_status),
              (AGENT_BUILDER, None)]
    for name, status in probes:
        state, detail = surface_reach(name, status)
        reach.append((name, state))
        emit = log.warning if state in FINDINGS else log.info
        emit("  %-14s %s  %-17s %s", name,
             "---" if status is None else status, state, detail)
        for line in repair_lines(state):
            emit("    repair: %s", line)
        if state in FINDINGS:
            findings += 1

    if evals:
        log.warning("%d eval(s) listed, and the listing carries the full "
                    "definition", len(evals))
        log.warning("  %s", export_command("evals"))
        for line in repair_lines("content-to-export"):
            log.warning("  repair: %s", line)
        findings += 1

    declared = list(args.prompt_id)
    declared += [p.strip() for p in
                 (os.environ.get("OPENAI_PROMPT_IDS") or "").split(",")
                 if p.strip()]
    if declared:
        log.info("%d declared prompt id(s)", len(declared))
    for pid in declared:
        text = pid.strip() if isinstance(pid, str) else pid
        status = None
        if isinstance(text, str) and text.startswith("pmpt_"):
            status, _ = get_json(session, "/prompts/" + text, key)
        state, detail = prompt_id_state(text, status)
        emit = log.warning if state in FINDINGS else log.info
        emit("  %-12s %s  %-16s %s", text,
             "---" if status is None else status, state, detail)
        if state == "readable":
            log.info("    %s", export_command("prompt", text))
        for line in repair_lines(state):
            emit("    repair: %s", line)
        if state in FINDINGS:
            findings += 1

    log.info("plan")
    for name, owner, line in export_plan(reach):
        log.info("  %-14s %-28s %s", name, owner, line)

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
