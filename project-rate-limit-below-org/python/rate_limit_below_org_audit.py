"""Find a container whose rate limit was set below the organization's.

Read only. Every request is a GET, on either or both providers:

  Anthropic, Admin API key
    GET /v1/organizations/rate_limits
    GET /v1/organizations/workspaces
    GET /v1/organizations/workspaces/{workspace_id}/rate_limits
  OpenAI, admin key
    GET /v1/organization/projects
    GET /v1/organization/projects/{project_id}/rate_limits

Nothing here reads a response header and nothing here reads traffic. The subject
is the configured ceiling on a container, which is legible whether or not that
container has sent a single request this month.

Anthropic returns each workspace override with org_limit beside value on the
same object, so the comparison is exact. OpenAI's project.rate_limit object
carries no organization value at all, so the peer maximum across projects stands
in for the tier and is reported as the proxy it is.

The repair is a write on both providers. It is printed, never performed.
"""
import argparse
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rate_limit_below_org_audit")

ANTHROPIC = "https://api.anthropic.com/v1"
OPENAI = "https://api.openai.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# The three limiters a model group carries. Other group types carry their own
# types (enqueued_batch_requests and so on), which is why nothing here assumes
# this tuple is the whole vocabulary: it is only used to order output.
LIMITER_ORDER = ("requests_per_minute", "input_tokens_per_minute",
                 "output_tokens_per_minute")

# Severity order, worst first. verdict() walks this, so adding a state means
# deciding where it sits rather than hoping the dict happened to be ordered.
SEVERITY = ("throttled-below-org", "override-pinned-at-org", "override-above-org",
            "limiter-inherited", "org-limit-unknown", "override-in-range",
            "no-override")

FINDINGS = ("throttled-below-org", "override-pinned-at-org", "override-above-org",
            "project-outlier")


def num(value):
    """An int, or None. Pure.

    None is a real answer here and must survive: org_limit is documented as
    nullable, and coercing a null to 0 would turn "cannot be graded" into
    "throttled to nothing", which is the loudest possible wrong answer.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def group_label(entry):
    """A stable printable name for one rate limit group. Pure.

    The organization endpoint and the workspace endpoint describe the same
    groups with the same models list, so labelling them identically is what lets
    the two be joined without matching on model strings by hand.
    """
    entry = entry or {}
    gtype = str(entry.get("group_type") or "").strip() or "unknown_group"
    models = sorted(str(m) for m in (entry.get("models") or []) if m)
    if not models:
        return gtype
    extra = len(models) - 1
    return "%s:%s%s" % (gtype, models[0], (" +%d" % extra) if extra else "")


def limits_of(entry):
    """{limiter_type: value} for one group entry. Pure. Unparseable values drop."""
    out = {}
    for row in ((entry or {}).get("limits") or []):
        row = row or {}
        ltype = str(row.get("type") or "").strip()
        value = num(row.get("value"))
        if ltype and value is not None:
            out[ltype] = value
    return out


def org_index(pages):
    """{group_label: {limiter_type: value}} from the organization endpoint. Pure.

    Takes an iterable of page payloads so a paginated read folds without the
    caller flattening first.
    """
    out = {}
    for page in pages or []:
        for entry in ((page or {}).get("data") or []):
            out.setdefault(group_label(entry), {}).update(limits_of(entry))
    return out


def overrides_of(entry):
    """[(limiter_type, value, org_limit)] for one workspace group. Pure.

    org_limit stays None when the API reports null. The caller decides whether
    to fall back to the organization listing; this function does not guess.
    """
    out = []
    for row in ((entry or {}).get("limits") or []):
        row = row or {}
        ltype = str(row.get("type") or "").strip()
        if not ltype:
            continue
        out.append((ltype, num(row.get("value")), num(row.get("org_limit"))))
    out.sort(key=lambda r: (LIMITER_ORDER.index(r[0])
                            if r[0] in LIMITER_ORDER else len(LIMITER_ORDER), r[0]))
    return out


def grade_override(value, org_limit, floor=0.5):
    """Grade one workspace limiter against the organization value. Pure.

    Returns (state, detail). The equality case is deliberately not folded into
    the ratio: an override equal to today's organization value is a pin, and a
    threshold check that passes everything at 1.0 will never say so.
    """
    if value is None:
        return ("no-override", "inherits the organization value")
    if org_limit is None:
        return ("org-limit-unknown",
                "value is %s and the organization publishes no number for this "
                "limiter, so the override cannot be graded" % fmt(value))
    if value <= 0:
        return ("throttled-below-org",
                "set to %s, which stops this limiter in this container entirely"
                % fmt(value))
    if value > org_limit:
        return ("override-above-org",
                "%s is above the organization's %s, and the organization limit "
                "applies anyway" % (fmt(value), fmt(org_limit)))
    if value == org_limit:
        return ("override-pinned-at-org",
                "%s, equal to the organization value today, so it will not "
                "follow the next increase" % fmt(value))
    share = float(value) / float(org_limit)
    if share <= floor:
        return ("throttled-below-org",
                "%s of %s (%.0f%%)" % (fmt(value), fmt(org_limit), share * 100))
    return ("override-in-range",
            "%s of %s (%.0f%%)" % (fmt(value), fmt(org_limit), share * 100))


def inherited_limiters(entry, org_types):
    """Limiter types the organization publishes that this group did not override.

    Pure. Returns [(limiter_type, org_value)] in a stable order.
    """
    overridden = {t for t, value, _ in overrides_of(entry) if value is not None}
    rows = [(t, v) for t, v in (org_types or {}).items() if t not in overridden]
    rows.sort(key=lambda r: (LIMITER_ORDER.index(r[0])
                             if r[0] in LIMITER_ORDER else len(LIMITER_ORDER), r[0]))
    return rows


def verdict(states):
    """Roll one container's limiter states into a single word. Pure."""
    present = set(states or [])
    for state in SEVERITY:
        if state in present:
            return state
    return "no-override"


def openai_matrix(by_project):
    """{model: {project_id: {"rpm": int|None, "tpm": int|None}}}. Pure.

    Rows with no model string are dropped rather than collected under a blank
    key, because a blank key would then be compared against real ones.
    """
    out = {}
    for pid, rows in sorted((by_project or {}).items()):
        for row in rows or []:
            row = row or {}
            model = str(row.get("model") or "").strip()
            if not model:
                continue
            out.setdefault(model, {})[str(pid)] = {
                "rpm": num(row.get("max_requests_per_1_minute")),
                "tpm": num(row.get("max_tokens_per_1_minute")),
            }
    return out


def openai_outliers(matrix, floor=0.5):
    """[(model, project_id, dimension, value, peer_max)]. Pure. Worst first.

    A model row carried by fewer than two projects is skipped: with one project
    there is no peer to compare against, and the object carries no organization
    value to compare against instead.
    """
    out = []
    for model, projects in sorted((matrix or {}).items()):
        if len(projects) < 2:
            continue
        for dim in ("rpm", "tpm"):
            values = {p: (v or {}).get(dim) for p, v in projects.items()}
            usable = {p: v for p, v in values.items() if v is not None and v > 0}
            if len(usable) < 2:
                continue
            peer_max = max(usable.values())
            for pid, value in sorted(usable.items()):
                if value <= peer_max * floor:
                    out.append((model, pid, dim, value, peer_max))
    out.sort(key=lambda r: (r[3] / float(r[4]), r[0], r[1], r[2]))
    return out


def fmt(value):
    """Thousands separators, or a dash for None. Pure."""
    if value is None:
        return "-"
    return "{:,}".format(int(value))


def repair_lines(state):
    """The repair for one state. Pure. Printed, never performed."""
    if state == "throttled-below-org":
        return ["this container is capped well under the organization ceiling. "
                "On Anthropic open the workspace in the Console, Rate limits "
                "tab, and raise or remove the override; there is no write "
                "endpoint for it.",
                "check the container id against what production actually uses "
                "before raising anything. A staging id that followed the code "
                "into production is repaired by changing the id, not the limit."]
    if state == "override-pinned-at-org":
        return ["an override equal to today's organization value is a pin, not "
                "a no-op. Delete the override so the container follows the next "
                "tier increase instead of staying on this number.",
                "if the equality is deliberate, write it down somewhere the "
                "next tier increase will be read, because nothing in the API "
                "will mention it again."]
    if state == "override-above-org":
        return ["an override above the organization value has no effect: "
                "organization limits always apply. Remove it so the "
                "configuration says what is actually enforced."]
    if state == "project-outlier":
        return ["raise it with the admin update call at "
                "/v1/organization/projects/{project_id}/rate_limits/"
                "{rate_limit_id}, sending the dimension you want changed. That "
                "is a write and this script does not make it.",
                "the peer maximum is a proxy for the tier value, not the tier "
                "value: this object carries no organization number. Confirm "
                "against the tier before treating the gap as the whole story."]
    if state == "org-limit-unknown":
        return ["the organization publishes no number for this limiter, so the "
                "override is unjudgeable rather than fine. Read "
                "/v1/organizations/rate_limits for the group before acting."]
    return []


def get(session, url, headers=None, **params):
    r = session.get(url, params=params, headers=headers or {}, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from %s: this path needs an organization scoped "
                         "read credential, not a workspace or project key"
                         % (r.status_code, url))
    r.raise_for_status()
    return r.json()


def anthropic_pages(session, path, **params):
    """Walk an Anthropic next_page cursor listing, yielding whole payloads."""
    params = dict(params)
    for _ in range(50):
        page = get(session, ANTHROPIC + path, **params)
        yield page
        nxt = page.get("next_page")
        if not nxt:
            return
        params["page"] = nxt


def anthropic_cursor(session, path, **params):
    """Walk an Anthropic after_id listing, yielding items."""
    params = dict(params)
    for _ in range(50):
        page = get(session, ANTHROPIC + path, **params)
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params["after_id"] = page.get("last_id") or (data[-1] or {}).get("id")


def openai_cursor(session, path, **params):
    """Walk an OpenAI after/last_id listing, yielding items."""
    params = dict(params)
    for _ in range(50):
        page = get(session, OPENAI + path, **params)
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params["after"] = page.get("last_id") or (data[-1] or {}).get("id")


def audit_anthropic(key, floor):
    s = requests.Session()
    s.headers.update({"x-api-key": key,
                      "anthropic-version": ANTHROPIC_VERSION,
                      "User-Agent": "rate-limit-below-org-audit/1.0"})
    org = org_index(anthropic_pages(s, "/organizations/rate_limits"))
    spaces = list(anthropic_cursor(s, "/organizations/workspaces", limit=100))
    log.info("anthropic: %d workspace(s), %d organization rate limit group(s)",
             len(spaces), len(org))

    findings = 0
    for space in spaces:
        wid = (space or {}).get("id") or "?"
        name = (space or {}).get("name") or "(unnamed)"
        entries = []
        for page in anthropic_pages(
                s, "/organizations/workspaces/%s/rate_limits" % wid):
            entries.extend(page.get("data") or [])
        if not entries:
            log.info("%-22s %s %s: inherits every organization limit",
                     "no-override", wid, name)
            continue
        for entry in entries:
            label = group_label(entry)
            org_types = org.get(label) or {}
            states = []
            rows = []
            for ltype, value, org_limit in overrides_of(entry):
                fallback = org_limit if org_limit is not None else org_types.get(ltype)
                state, detail = grade_override(value, fallback, floor)
                states.append(state)
                rows.append((ltype, state, detail))
            inherited = inherited_limiters(entry, org_types)
            if inherited and states:
                states.append("limiter-inherited")
            state = verdict(states)
            emit = log.warning if state in FINDINGS else log.info
            emit("%-22s %s %s / %s", state, wid, name, label)
            for ltype, row_state, detail in rows:
                emit("  %-26s %s", ltype, detail)
            for ltype, value in inherited:
                emit("  inherited: %s (%s from the organization)", ltype, fmt(value))
            for line in repair_lines(state):
                emit("  repair: %s", line)
            if state in FINDINGS:
                findings += 1
    return findings


def audit_openai(key, floor):
    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + key,
                      "User-Agent": "rate-limit-below-org-audit/1.0"})
    projects = list(openai_cursor(s, "/organization/projects", limit=100,
                                  include_archived="false"))
    by_project = {}
    names = {}
    for project in projects:
        pid = (project or {}).get("id") or "?"
        names[pid] = (project or {}).get("name") or "(unnamed)"
        by_project[pid] = list(openai_cursor(
            s, "/organization/projects/%s/rate_limits" % pid, limit=100))

    matrix = openai_matrix(by_project)
    comparable = sum(1 for m in matrix.values() if len(m) >= 2)
    log.info("openai: %d project(s), %d model row(s) carried by 2 or more "
             "projects", len(projects), comparable)
    if len(projects) < 2:
        log.info("%-22s one project only: this object carries no organization "
                 "value, so there is nothing to compare against", "no-peer")
        return 0

    rows = openai_outliers(matrix, floor)
    dimension = {"rpm": "max_requests_per_1_minute",
                 "tpm": "max_tokens_per_1_minute"}
    seen = set()
    for model, pid, dim, value, peer_max in rows:
        log.warning("%-22s %s %s  %s", "project-outlier", pid,
                    names.get(pid, "(unnamed)"), model)
        log.warning("  %-26s %s against a peer maximum of %s (%.0f%%)",
                    dimension[dim], fmt(value), fmt(peer_max),
                    100.0 * value / peer_max)
        seen.add((pid, model))
    if rows:
        for line in repair_lines("project-outlier"):
            log.warning("  repair: %s", line)
    return len(seen)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--floor", type=float, default=0.5,
                    help="report an override at or below this share of the "
                         "organization value (default 0.5)")
    args = ap.parse_args()

    anthropic_key = os.environ.get("ANTHROPIC_ADMIN_KEY")
    openai_key = os.environ.get("OPENAI_ADMIN_KEY")
    if not anthropic_key and not openai_key:
        log.error("set ANTHROPIC_ADMIN_KEY, OPENAI_ADMIN_KEY, or both. Each "
                  "must be an organization scoped read credential; a workspace "
                  "or project key cannot reach these paths")
        return 2

    findings = 0
    if anthropic_key:
        findings += audit_anthropic(anthropic_key, args.floor)
    if openai_key:
        findings += audit_openai(openai_key, args.floor)

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
