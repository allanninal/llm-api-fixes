"""Find OpenAI projects whose retention posture is not the one you claim.

Read only. One GET for the organization default, one paged GET for the project
list, and one GET per project. Every request is a GET and no request body is
constructed anywhere, including for the repair, which is printed as text.

The two levels disagree more often than anyone expects, and the interesting
answer is the resolution: what a project actually gets, and whether anything on
the project holds it there.
"""
import argparse
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_data_retention_audit")

API = "https://api.openai.com/v1"

ZDR = ("zero_data_retention", "enhanced_zero_data_retention")
MAM = ("modified_abuse_monitoring", "enhanced_modified_abuse_monitoring")
INHERIT = "organization_default"
NO_CONTROL = "none"

# Families, not a ranking. Whether modified abuse monitoring satisfies a given
# commitment is a question about the commitment; the script will not answer it.
FAMILY_LABEL = {"zdr": "zero data retention",
                "modified-abuse-monitoring": "modified abuse monitoring"}

# What to write when a project has to be brought up to the claimed family. The
# enhanced variants are requested from OpenAI rather than set, and the printed
# repair says so.
TARGET = {"zdr": "zero_data_retention",
          "modified-abuse-monitoring": "modified_abuse_monitoring"}

FINDINGS = ("retention-unreadable", "no-retention-control", "weaker-than-claimed",
            "inherited-not-pinned")

SEVERITY = {"no-retention-control": 0, "weaker-than-claimed": 1,
            "retention-unreadable": 2, "inherited-not-pinned": 3}


def family(retention_type):
    """Group one type value into a family. Pure. Never ranks families."""
    t = str(retention_type or "").strip().lower()
    if not t:
        return "unreadable"
    if t in ZDR:
        return "zdr"
    if t in MAM:
        return "modified-abuse-monitoring"
    if t == NO_CONTROL:
        return "none"
    return "unrecognised"


def effective(org_type, project_type):
    """(type, inherited) for one project. Pure.

    organization_default is the whole reason this function exists: the project
    reports a word rather than a posture, and the posture is one level up.
    """
    t = str(project_type or "").strip().lower()
    if not t:
        return (None, False)
    if t == INHERIT:
        return (str(org_type or "").strip().lower() or None, True)
    return (t, False)


def archived(project):
    """Is this project archived? Pure. Both signals, because they disagree."""
    row = project or {}
    return bool(row.get("archived_at")) or str(row.get("status") or "") == "archived"


def classify(project, org_type, project_type, require="zdr"):
    """Classify one project's retention. Pure. Returns (state, detail).

    Order matters: unreadable, then none, then family, then inheritance. An
    unrecognised value is never graded as safe, and none is never summarised
    into the same row as an inherit even though they sit in the same enum.
    """
    eff, inherited = effective(org_type, project_type)
    fam = family(eff)
    tail = " (archived, and its retained data is still retained)" if archived(project) else ""
    want = FAMILY_LABEL.get(require, require)

    if fam in ("unreadable", "unrecognised"):
        return ("retention-unreadable",
                "the project reports %s, which this audit will not grade as safe%s"
                % (repr(str(project_type)) if project_type else "nothing", tail))
    if fam == "none":
        return ("no-retention-control",
                "type is none: no retention control at all, whatever the "
                "organization default says%s" % tail)
    if fam != require:
        return ("weaker-than-claimed",
                "resolves to %s (%s)%s, and %s was claimed"
                % (eff,
                   "inherited from the organization" if inherited
                   else "set on the project", tail, want))
    if inherited:
        return ("inherited-not-pinned",
                "resolves to %s only because the organization default says so. "
                "Nothing on the project pins it%s" % (eff, tail))
    return ("compliant", "pinned on the project at %s%s" % (eff, tail))


def residency_note(project, want):
    """(ok, detail) on the residency axis. Pure. Absent is unset, not GLOBAL."""
    if not want:
        return (True, None)
    got = (project or {}).get("residency")
    if got is None:
        return (False, "residency is unset on this project, which is neither "
                       "GLOBAL nor %s" % want)
    if str(got) != str(want):
        return (False, "residency is %s, and %s was claimed" % (got, want))
    return (True, None)


def repair_lines(state, project, require="zdr"):
    """The repair for one project. Pure. Printed, never performed."""
    pid = str((project or {}).get("id") or "unknown")
    lines = []
    if state not in FINDINGS:
        return lines
    if state == "inherited-not-pinned":
        lines.append("this resolves correctly today and moves the day somebody "
                     "changes the organization default. Pin it on the project if "
                     "the commitment is about this workload.")
    elif state == "retention-unreadable":
        lines.append("the endpoint returned a value this audit does not "
                     "recognise. Read it by hand before assuming anything.")
    target = TARGET.get(require)
    if target:
        lines.append("POST /v1/organization/projects/%s/data_retention with a body "
                     'of {"retention_type": "%s"}' % (pid, target))
        lines.append("the request field is retention_type; the response field is "
                     "type. A body copied from the read shape 400s.")
        lines.append("zero data retention and the enhanced variants are generally "
                     "enabled on the account by OpenAI rather than being "
                     "self-serve. Request it; do not assume the call will take.")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--require", default="zdr",
                    choices=sorted(FAMILY_LABEL),
                    help="the retention family your commitments claim")
    ap.add_argument("--residency", default=None,
                    help="the project residency your commitments claim, "
                         "e.g. EU_STORAGE_PROCESSING")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key; a project "
                  "key cannot read /v1/organization/data_retention")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})

    org = get(s, "/organization/data_retention") or {}
    org_type = org.get("type")
    log.info("organization default: %s", org_type or "unreadable")

    projects = list(paged(s, "/organization/projects", limit=100,
                          include_archived="true"))
    findings = []
    for project in projects:
        pid = str(project.get("id") or "")
        block = get(s, "/organization/projects/%s/data_retention" % pid) or {}
        state, detail = classify(project, org_type, block.get("type"), args.require)
        if state in FINDINGS:
            findings.append((project, state, detail))

    residency_bad = []
    if args.residency:
        for project in projects:
            ok, detail = residency_note(project, args.residency)
            if not ok:
                residency_bad.append((project, detail))

    log.info("%d project(s), %d retention finding(s), %d residency finding(s)",
             len(projects), len(findings), len(residency_bad))

    findings.sort(key=lambda r: (SEVERITY.get(r[1], 9), str(r[0].get("name") or "")))
    for project, state, detail in findings:
        log.warning("%-22s %-14s %s", state, project.get("id"),
                    project.get("name") or "(unnamed)")
        log.warning("  %s", detail)
        for line in repair_lines(state, project, args.require):
            log.warning("  repair: %s", line)

    for project, detail in residency_bad:
        log.warning("%-22s %-14s %s", "residency", project.get("id"), detail)

    return 1 if (findings or residency_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
