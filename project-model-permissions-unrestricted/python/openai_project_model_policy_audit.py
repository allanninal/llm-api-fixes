"""Find OpenAI projects whose model permission policy excludes nothing.

Read only. One paged GET for the project list, two GETs per project for the
permission objects, and five usage reads. Every request is a GET and no request
body is constructed; the least-privilege policy is printed as text.

This script has no opinion about which model is appropriate for which workload.
It answers one structural question: is there a policy here, and has it ever
excluded anything the project wanted.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_project_model_policy_audit")

API = "https://api.openai.com/v1"
DAY = 86400

# The hosted tools, and the usage endpoint that can count each one. mcp has no
# usage endpoint, so it is reported as uncountable rather than as unused.
TOOL_USAGE = {
    "web_search": ("/organization/usage/web_search_calls", "num_requests"),
    "code_interpreter": ("/organization/usage/code_interpreter_sessions", "num_sessions"),
    "file_search": ("/organization/usage/file_search_calls", "num_requests"),
    "image_generation": ("/organization/usage/images", "num_model_requests"),
}

FINDINGS = ("no-policy", "deny-list-empty", "allow-list-empty",
            "deny-list-fails-open", "allow-list-wider-than-use",
            "policy-unreadable")

SEVERITY = {"deny-list-empty": 0, "no-policy": 1, "allow-list-wider-than-use": 2,
            "deny-list-fails-open": 3, "allow-list-empty": 4,
            "policy-unreadable": 5}


def policy_ids(policy):
    """The non-empty model ids on a policy. Pure."""
    out = []
    for value in (policy or {}).get("model_ids") or []:
        text = str(value or "").strip()
        if text:
            out.append(text)
    return out


def policy_state(policy):
    """Shape of one model permissions object. Pure.

    absent | deny-empty | deny-list | allow-empty | allow-list | unreadable.
    An absent policy and an empty deny list permit exactly the same set and are
    still two different states, because only one of them looks configured.
    """
    if policy is None:
        return "absent"
    mode = str((policy or {}).get("mode") or "").strip().lower()
    ids = policy_ids(policy)
    if mode == "deny_list":
        return "deny-empty" if not ids else "deny-list"
    if mode == "allow_list":
        return "allow-empty" if not ids else "allow-list"
    return "unreadable"


def unrestricted(policy):
    """Does this policy permit every model? Pure. Narrow on purpose."""
    return policy_state(policy) in ("absent", "deny-empty")


def fold_models(buckets, count_field="num_model_requests"):
    """{project_id: {model: requests}} across usage buckets. Pure."""
    out = {}
    for bucket in buckets or []:
        for result in (bucket or {}).get("results") or []:
            row = result or {}
            try:
                n = int(row.get(count_field) or 0)
            except (TypeError, ValueError):
                n = 0
            if n <= 0:
                continue
            pid = str(row.get("project_id") or "unattributed")
            model = str(row.get("model") or "unknown")
            entry = out.setdefault(pid, {})
            entry[model] = entry.get(model, 0) + n
    return out


def unused_allowed(policy, used):
    """Allow-listed models that served nothing in the window. Pure.

    Only meaningful for an allow list. A deny list says nothing about what a
    project is permitted to reach, so subtracting usage from it is nonsense.
    """
    if policy_state(policy) != "allow-list":
        return []
    return sorted(set(policy_ids(policy)) - set((used or {}).keys()))


def unused_tools(perms, counts):
    """[(tool, why)] for enabled hosted tools. Pure.

    A tool with no usage endpoint is reported as uncountable. Treating its
    absence from a report as zero usage would be inventing evidence.
    """
    out = []
    for tool, block in sorted((perms or {}).items()):
        if not isinstance(block, dict) or not block.get("enabled"):
            continue
        if tool not in TOOL_USAGE:
            out.append((tool, "enabled, and no usage endpoint counts it"))
            continue
        if int((counts or {}).get(tool) or 0) <= 0:
            out.append((tool, "enabled, and %s reports nothing in the window"
                        % TOOL_USAGE[tool][0].rsplit("/", 1)[-1]))
    return out


def classify(policy, used, days=30):
    """Classify one project's model policy. Pure. Returns (state, detail)."""
    shape = policy_state(policy)
    seen = sorted((used or {}).keys())

    if shape == "absent":
        return ("no-policy",
                "no model permissions policy is configured; every model the "
                "organization is entitled to is reachable from this project")
    if shape == "unreadable":
        return ("policy-unreadable",
                "the policy object has no recognisable mode and will not be "
                "graded as restrictive")
    if shape == "deny-empty":
        return ("deny-list-empty",
                "a policy object exists, mode is deny_list, and model_ids is "
                "empty. This permits every model and looks configured")
    if shape == "allow-empty":
        return ("allow-list-empty",
                "mode is allow_list with no model_ids, which permits nothing. If "
                "this project is serving traffic, something else is going on")
    if shape == "deny-list":
        return ("deny-list-fails-open",
                "deny_list naming %d model(s). Restrictive today and open by "
                "construction to anything released tomorrow"
                % len(policy_ids(policy)))

    spare = unused_allowed(policy, used)
    if spare:
        return ("allow-list-wider-than-use",
                "allow_list names %d model(s); %d served any request in the last "
                "%d day(s). Unused: %s"
                % (len(policy_ids(policy)), len(seen), days, ", ".join(spare)))
    return ("restricted",
            "allow_list of %d model(s), all of them in use"
            % len(policy_ids(policy)))


def repair_lines(state, project_id, used):
    """The repair for one project. Pure. Printed, never performed.

    The suggested allow list is exactly the set of models the project already
    called. The script never proposes a model the project has not used.
    """
    lines = []
    if state not in FINDINGS:
        return lines
    if state == "no-policy":
        lines.append("add the policy call to whatever creates projects. It does "
                     "not inherit from the organization or from any other "
                     "project.")
    elif state == "deny-list-empty":
        lines.append("somebody opened this policy and did not finish it. Find out "
                     "who, and whether anything downstream assumed it was done.")
    elif state == "deny-list-fails-open":
        lines.append("a deny list permits every model that does not exist yet. "
                     "Switch to an allow list unless keeping one named model out "
                     "is genuinely the whole requirement.")
    elif state == "allow-list-empty":
        lines.append("this permits nothing. Read it before changing it; an empty "
                     "allow list is more often a mistake than a lockdown.")
    elif state == "policy-unreadable":
        lines.append("read the policy object by hand. This audit will not call an "
                     "unrecognised mode restrictive.")
    observed = sorted((used or {}).keys())
    if observed:
        lines.append('POST /v1/organization/projects/%s/model_permissions with '
                     '{"mode": "allow_list", "model_ids": %s}'
                     % (project_id, list(observed)))
        lines.append("that list is what this project already called in the "
                     "window. It is a starting point, not a recommendation about "
                     "which model suits the work.")
    else:
        lines.append("this project called no model in the window, so there is no "
                     "observed set to build an allow list from. Decide it "
                     "deliberately rather than copying another project.")
    return lines


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an organization "
                         "admin key, not a project key" % r.status_code)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def paged(session, path, **params):
    params = dict(params)
    while True:
        page = get(session, path, params) or {}
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params["after"] = page.get("last_id") or (data[-1] or {}).get("id")


def usage(session, path, start, end):
    params = {"start_time": start, "end_time": end, "bucket_width": "1d",
              "limit": 31, "group_by": ["project_id", "model"]}
    out = []
    while True:
        page = get(session, path, params) or {}
        out.extend(page.get("data") or [])
        cursor = page.get("next_page")
        if not page.get("has_more") or not cursor:
            return out
        params = dict(params, page=cursor)


def tool_counts(session, start, end):
    """{project_id: {tool: count}} for the tools that have a usage endpoint."""
    out = {}
    for tool, (path, field) in TOOL_USAGE.items():
        params = {"start_time": start, "end_time": end, "bucket_width": "1d",
                  "limit": 31, "group_by": ["project_id"]}
        page = get(session, path, params) or {}
        for bucket in page.get("data") or []:
            for result in (bucket or {}).get("results") or []:
                row = result or {}
                try:
                    n = int(row.get(field) or 0)
                except (TypeError, ValueError):
                    n = 0
                pid = str(row.get("project_id") or "unattributed")
                out.setdefault(pid, {})
                out[pid][tool] = out[pid].get(tool, 0) + n
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="usage window to read")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key; a project "
                  "key cannot read the per-project permission endpoints")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})
    end = int(time.time())
    start = end - max(1, args.days) * DAY

    used = fold_models(usage(s, "/organization/usage/completions", start, end))
    counts = tool_counts(s, start, end)
    projects = list(paged(s, "/organization/projects", limit=100))

    findings, tool_findings = [], []
    for project in projects:
        pid = str(project.get("id") or "")
        policy = get(s, "/organization/projects/%s/model_permissions" % pid)
        state, detail = classify(policy, used.get(pid), args.days)
        if state in FINDINGS:
            findings.append((project, state, detail))
        perms = get(s, "/organization/projects/%s/hosted_tool_permissions" % pid) or {}
        spare = unused_tools(perms, counts.get(pid))
        if spare:
            tool_findings.append((project, spare))

    log.info("%d project(s), %d policy finding(s), %d project(s) with unused "
             "hosted tools", len(projects), len(findings), len(tool_findings))

    findings.sort(key=lambda r: (SEVERITY.get(r[1], 9), str(r[0].get("name") or "")))
    for project, state, detail in findings:
        pid = str(project.get("id") or "")
        log.warning("%-26s %-14s %s", state, pid, project.get("name") or "(unnamed)")
        log.warning("  %s", detail)
        for line in repair_lines(state, pid, used.get(pid)):
            log.warning("  repair: %s", line)

    for project, spare in tool_findings:
        log.warning("%-26s %-14s %s", "hosted tools", project.get("id"),
                    project.get("name") or "(unnamed)")
        for tool, why in spare:
            log.warning("  %s: %s", tool, why)

    return 1 if (findings or tool_findings) else 0


if __name__ == "__main__":
    sys.exit(main())
