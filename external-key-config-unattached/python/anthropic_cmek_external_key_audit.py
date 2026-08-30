"""Find Anthropic CMEK key configs that are not encrypting anything.

Read only. Two paged GETs against /v1/organizations/external_keys and
/v1/organizations/workspaces with an Admin API key. Every request is a GET.

The external keys resource offers a validate call. It is a write verb, so this
script does not use it, and the repair for an unattached config is printed for
a human to run rather than performed.

Nothing secret is printed. Provider coordinates are resource identifiers rather
than credentials, and the AWS account id inside an ARN is masked anyway.
"""
import argparse
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_cmek_external_key_audit")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

FINDINGS = ("unattached-and-unused", "unattached-but-referenced",
            "archived-workspaces-only", "attached-nothing-visible",
            "geo-mismatch", "attachment-unreadable")

SEVERITY = {"unattached-and-unused": 0, "geo-mismatch": 1,
            "unattached-but-referenced": 2, "attached-nothing-visible": 3,
            "archived-workspaces-only": 4, "attachment-unreadable": 5}


def attachment_type(key):
    """"attached" / "unattached" / "unknown". Pure.

    Anything else is unknown rather than assumed unattached: guessing wrong in
    that direction produces a report telling somebody to delete a live key.
    """
    kind = str(((key or {}).get("attachment") or {}).get("type") or "").strip().lower()
    return kind if kind in ("attached", "unattached") else "unknown"


def mask_arn(arn):
    """Hide the account id in an AWS ARN. Pure. Non-ARNs pass through."""
    text = str(arn or "")
    parts = text.split(":")
    if len(parts) < 6 or parts[0] != "arn":
        return text or "unknown"
    parts[4] = "****"
    return ":".join(parts)


def kms_ref(provider_config):
    """One short line naming the KMS key. Pure. No credentials, ever."""
    cfg = provider_config or {}
    kind = str(cfg.get("type") or "").strip().lower()
    if kind == "aws":
        return "aws " + mask_arn(cfg.get("kms_arn"))
    if kind == "gcp":
        return "gcp " + str(cfg.get("key_name") or "unknown")
    if kind == "azure":
        return "azure %s in %s" % (cfg.get("key_name") or "unknown",
                                   cfg.get("vault_uri") or "unknown vault")
    return "unrecognised provider %s" % (kind or "none")


def workspace_geo(workspace):
    """The workspace's storage geo, or None. Pure."""
    geo = ((workspace or {}).get("data_residency") or {}).get("workspace_geo")
    return str(geo) if geo else None


def coverage(workspaces):
    """{external_key_id: {"live": [ids], "archived": [ids]}}. Pure.

    Built from the workspaces, because the attachment object carries only its
    own type and no list of what uses it.
    """
    out = {}
    for workspace in workspaces or []:
        row = workspace or {}
        key_id = row.get("external_key_id")
        if not key_id:
            continue
        entry = out.setdefault(str(key_id), {"live": [], "archived": []})
        bucket = "archived" if row.get("archived_at") else "live"
        entry[bucket].append(str(row.get("id") or "unknown"))
    for entry in out.values():
        entry["live"].sort()
        entry["archived"].sort()
    return out


def uncovered(workspaces):
    """(live, archived) workspace ids with no external_key_id at all. Pure."""
    live, archived = [], []
    for workspace in workspaces or []:
        row = workspace or {}
        if row.get("external_key_id"):
            continue
        (archived if row.get("archived_at") else live).append(
            str(row.get("id") or "unknown"))
    return (sorted(live), sorted(archived))


def classify(key, cover, geos):
    """Classify one key config. Pure. Returns (state, detail).

    cover: {"live": [ids], "archived": [ids]} from the workspace listing.
    geos:  [(workspace_id, workspace_geo)] for the workspaces that name it.
    """
    kind = attachment_type(key)
    live = list((cover or {}).get("live") or [])
    archived = list((cover or {}).get("archived") or [])

    if kind == "unknown":
        return ("attachment-unreadable",
                "attachment.type is not attached or unattached, so this audit "
                "will not say whether the config is in use")

    if kind == "unattached":
        if live or archived:
            return ("unattached-but-referenced",
                    "the config reports unattached while %d workspace(s) name it "
                    "(%s). The two listings disagree"
                    % (len(live) + len(archived), ", ".join(live + archived)))
        return ("unattached-and-unused",
                "attachment.type is unattached and no workspace names it. The API "
                "describes this state as inert: it takes part in no encryption "
                "path")

    if not live and not archived:
        return ("attached-nothing-visible",
                "reported attached, and no workspace this key can enumerate names "
                "it. An attachment you cannot see is still an attachment")
    if not live:
        return ("archived-workspaces-only",
                "attached, and the only workspaces naming it are archived (%s). "
                "Their retained data is still encrypted under this config"
                % ", ".join(archived))

    want = str((key or {}).get("geo") or "")
    mismatched = [(w, g) for w, g in (geos or []) if g and want and str(g) != want]
    if mismatched:
        return ("geo-mismatch",
                "config geo is %s and it covers %s"
                % (want, ", ".join("%s at %s" % pair for pair in mismatched)))

    return ("covered",
            "attached, covering %d live workspace(s)%s"
            % (len(live), " and %d archived" % len(archived) if archived else ""))


def repair_lines(state, key):
    """The repair for one key config. Pure. Printed, never performed.

    A delete is printed for exactly one state. external_key_id is write-once on
    a workspace, so no repair here proposes re-pointing a workspace at a
    different config: that is not something the API allows.
    """
    key_id = str((key or {}).get("id") or "unknown")
    lines = []
    if state not in FINDINGS:
        return lines
    if state == "unattached-and-unused":
        lines.append("attach it to the workspace it was made for. Attachment is "
                     "the step that makes a config live; creating it is not.")
        lines.append("if it was superseded, it can be deleted: DELETE "
                     "/v1/organizations/external_keys/%s. Nothing depends on it."
                     % key_id)
    elif state == "unattached-but-referenced":
        lines.append("do not delete this. Two listings disagree, and the safe "
                     "reading is the one that says something is using it.")
    elif state == "archived-workspaces-only":
        lines.append("do not delete this. Deleting a config an archived workspace "
                     "depends on makes that workspace's retained data "
                     "unrecoverable.")
    elif state == "attached-nothing-visible":
        lines.append("the coverage map is incomplete rather than empty. Widen the "
                     "workspace listing before concluding anything about this "
                     "config.")
    elif state == "geo-mismatch":
        lines.append("a workspace cannot be re-pointed: external_key_id is "
                     "write-once and cannot be detached or replaced. Resolve this "
                     "against the residency commitment, not by swapping keys.")
    else:
        lines.append("read this config by hand. The attachment discriminator was "
                     "not one of the two values this audit recognises.")
    lines.append("the validate call on this resource is a write verb and this "
                 "script does not use it. Run it deliberately if you need it.")
    return lines


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401,):
        raise SystemExit("401 from Anthropic: /v1/organizations/* needs an Admin "
                         "API key, not a workspace key")
    if r.status_code in (403, 404):
        return None
    r.raise_for_status()
    return r.json()


def paged_cursor(session, path, **params):
    """external_keys pagination: next_page fed back as the page parameter."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        if page is None:
            return
        for item in page.get("data") or []:
            yield item
        cursor = page.get("next_page")
        if not page.get("has_more") or not cursor:
            return
        params = dict(params, page=cursor)


def paged_after_id(session, path, **params):
    """workspaces pagination: after_id, a different cursor in the same script."""
    params = dict(params)
    while True:
        page = get(session, path, params) or {}
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params["after_id"] = page.get("last_id") or (data[-1] or {}).get("id")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--geo", default=None,
                    help="the storage geo your residency commitment claims")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key; a workspace key "
                  "cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    keys = list(paged_cursor(s, "/organizations/external_keys",
                             beta="true", limit=100))
    if not keys:
        probe = get(s, "/organizations/external_keys", {"beta": "true", "limit": 1})
        if probe is None:
            log.info("the external keys endpoint is not available to this "
                     "organization. CMEK is a beta enterprise feature and this is "
                     "an answer, not a finding.")
            return 0

    workspaces = list(paged_after_id(s, "/organizations/workspaces",
                                     beta="true", limit=100,
                                     include_archived="true"))
    cover = coverage(workspaces)
    by_id = {str((w or {}).get("id")): w for w in workspaces}

    findings = []
    for key in keys:
        key_id = str(key.get("id") or "")
        entry = cover.get(key_id) or {}
        geos = [(w, workspace_geo(by_id.get(w)))
                for w in (entry.get("live") or []) + (entry.get("archived") or [])]
        state, detail = classify(key, entry, geos)
        if state in FINDINGS:
            findings.append((key, state, detail))

    live_bare, archived_bare = uncovered(workspaces)

    log.info("%d external key config(s), %d workspace(s), %d finding(s)",
             len(keys), len(workspaces), len(findings))

    findings.sort(key=lambda r: (SEVERITY.get(r[1], 9), str(r[0].get("id") or "")))
    for key, state, detail in findings:
        log.warning("%-26s %-12s %s", state, key.get("id"),
                    key.get("display_name") or "(unnamed)")
        log.warning("  %s", detail)
        log.warning("  provider: %s", kms_ref(key.get("provider_config")))
        for line in repair_lines(state, key):
            log.warning("  repair: %s", line)

    if live_bare:
        log.warning("uncovered: %d of %d workspace(s) have no external_key_id at "
                    "all (%s)", len(live_bare), len(workspaces),
                    ", ".join(live_bare))
    if archived_bare:
        log.info("uncovered and archived: %d workspace(s) (%s)",
                 len(archived_bare), ", ".join(archived_bare))
    if args.geo:
        log.info("claimed storage geo: %s", args.geo)
        for workspace in workspaces:
            got = workspace_geo(workspace)
            if got and got != args.geo:
                log.warning("residency  %-12s workspace_geo is %s, and %s was "
                            "claimed", workspace.get("id"), got, args.geo)

    return 1 if (findings or live_bare) else 0


if __name__ == "__main__":
    sys.exit(main())
