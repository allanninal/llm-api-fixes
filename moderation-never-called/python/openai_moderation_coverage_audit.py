"""Find an OpenAI organization whose moderation endpoint is never called.

Read only. Two paged GETs against /v1/organization/usage/moderations and
/v1/organization/usage/completions with an organization admin key. Every
request is a GET and no request body is constructed.

The script deliberately does not call the moderations endpoint to prove it
works. Sending content to a model to see what comes back is generating, and
nothing in this section generates. The finding comes entirely from two request
counts the organization already has.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_moderation_coverage_audit")

API = "https://api.openai.com/v1"
DAY = 86400

# The whole text-moderation-* family is retired: -latest, -stable and the
# pinned -006 / -007 snapshots. Matched by prefix so a pin is caught too.
RETIRED_PREFIX = "text-moderation"
CURRENT = "omni-moderation-latest"

FINDINGS = ("never-called", "retired-model-id", "thin-coverage")

# An unmoderated public surface outranks a stale model id, which outranks a
# ratio, because the ratio is the weakest of the three signals by some way.
SEVERITY = {"never-called": 0, "retired-model-id": 1, "thin-coverage": 2}


def fold(buckets, count_field="num_model_requests"):
    """{project_id: {"requests": n, "models": {id: n}}} across buckets. Pure.

    A result carrying zero requests creates no entry. That matters: the whole
    detection rests on a project being absent from the moderations fold, and a
    zero-valued row would make it present with a count of nothing.
    """
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
            entry = out.setdefault(pid, {"requests": 0, "models": {}})
            entry["requests"] += n
            entry["models"][model] = entry["models"].get(model, 0) + n
    return out


def is_retired(model):
    """Is this a retired moderation model id? Pure. Prefix match, so pins count."""
    return str(model or "").strip().lower().startswith(RETIRED_PREFIX)


def retired_ids(models):
    """Sorted retired ids inside one {model: requests} mapping. Pure."""
    return sorted(m for m in (models or {}) if is_retired(m))


def coverage(completions, moderations):
    """[(project_id, completions, moderations, models)] busiest first. Pure.

    Driven by the completions side, so a project with no moderations entry is
    still a row. Dropping it is exactly the bug this note is about.
    """
    rows = []
    for pid, entry in (completions or {}).items():
        mod = (moderations or {}).get(pid) or {}
        rows.append((pid,
                     int((entry or {}).get("requests") or 0),
                     int(mod.get("requests") or 0),
                     dict(mod.get("models") or {})))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def classify(row, min_completions=500, min_ratio=0.2):
    """Classify one coverage row. Pure. Returns (state, detail).

    The model ids are tested BEFORE any count. A project can be moderating
    every request it serves and still be a finding, because a healthy ratio on
    a retired id is exactly what a count-based audit calls fine.
    """
    pid, completions, moderations, models = row
    if completions < min_completions:
        return ("below-floor",
                "%d completion request(s), under the %d floor"
                % (completions, min_completions))

    retired = retired_ids(models)
    if retired:
        share = sum(models[m] for m in retired) / float(max(1, moderations))
        return ("retired-model-id",
                "%d moderation request(s), %d%% of them on %s"
                % (moderations, round(share * 100), ", ".join(retired)))

    if moderations <= 0:
        return ("never-called",
                "%d completion request(s) and no moderation request at all"
                % completions)

    ratio = moderations / float(completions)
    if ratio < min_ratio:
        return ("thin-coverage",
                "%d moderation request(s) against %d completion request(s), a "
                "ratio of %.2f" % (moderations, completions, ratio))
    return ("covered",
            "%d moderation request(s), ratio %.2f" % (moderations, ratio))


def repair_lines(state, row):
    """The repair for one classified project. Pure. Printed, never performed."""
    pid, completions, moderations, models = row
    lines = []
    if state not in FINDINGS:
        return lines
    if state == "never-called":
        lines.append("route user input through the moderations endpoint before "
                     "the completion. It bills nothing, so a round trip is the "
                     "entire cost.")
        lines.append("branch on flagged, and log category_scores rather than the "
                     "single boolean, so a threshold can be tuned per category "
                     "later without another deploy.")
    elif state == "retired-model-id":
        lines.append("move %s to %s, which is current and is the only moderation "
                     "model that reads images as well as text."
                     % (", ".join(retired_ids(models)), CURRENT))
        lines.append("if this product accepts uploads, the retired id has been "
                     "screening the text half only.")
    else:
        lines.append("moderation is being called on a small share of the traffic. "
                     "Find the call sites that skip it before tuning anything; "
                     "the ratio alone cannot tell you which they are.")
    lines.append("re-read project %s with the same two usage reports after the "
                 "deploy, and check the model column, not only the count" % pid)
    return lines


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: the usage reports need an organization "
                         "admin key with api.usage.read, not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def usage(session, path, start, end):
    """Every bucket in the window. Paginates on next_page via the page param."""
    params = {"start_time": start, "end_time": end, "bucket_width": "1d",
              "limit": 31, "group_by": ["project_id", "model"]}
    out = []
    while True:
        page = get(session, path, params)
        out.extend(page.get("data") or [])
        cursor = page.get("next_page")
        if not page.get("has_more") or not cursor:
            return out
        params = dict(params, page=cursor)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="window to read")
    ap.add_argument("--min-completions", type=int, default=500,
                    help="completion requests a project needs before it is graded")
    ap.add_argument("--min-ratio", type=float, default=0.2,
                    help="soft floor on moderations per completion")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key; a project "
                  "key cannot read /v1/organization/usage/*")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})
    end = int(time.time())
    start = end - max(1, args.days) * DAY

    completions = fold(usage(s, "/organization/usage/completions", start, end))
    moderations = fold(usage(s, "/organization/usage/moderations", start, end))

    rows = coverage(completions, moderations)
    graded = [(row, classify(row, args.min_completions, args.min_ratio))
              for row in rows]
    bad = [(row, state, detail) for row, (state, detail) in graded
           if state in FINDINGS]
    over_floor = sum(1 for _, (state, _) in graded if state != "below-floor")

    log.info("%d project(s) over the %d request floor, %d finding(s)",
             over_floor, args.min_completions, len(bad))

    bad.sort(key=lambda r: (SEVERITY.get(r[1], 9), -r[0][1]))
    for row, state, detail in bad:
        log.warning("%-18s %-14s %s", state, row[0], detail)
        for line in repair_lines(state, row):
            log.warning("  repair: %s", line)

    log.info("not graded: this report counts requests. Whether the code branched "
             "on flagged, and whether the input assessed was the user's, are not "
             "in the API.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
