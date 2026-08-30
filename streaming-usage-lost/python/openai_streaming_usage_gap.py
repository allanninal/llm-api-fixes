"""Reconcile OpenAI's token totals against the ones your own telemetry recorded.

Read only. Two GET requests against the organization endpoints and a JSON file
you supply. Those endpoints reject project keys, so this needs an organization
admin key (sk-admin-), which can and should be provisioned read-only.

The finding is a gap between two sources, not a problem with either provider's
billing. Streamed responses carry usage: null on every chunk unless the request
asked for the totals, so a dashboard built on per-request telemetry undercounts
by whatever share of the traffic streams. The repair is printed, not applied.
"""
import argparse
import json
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_streaming_usage_gap")

API = "https://api.openai.com/v1"

FINDINGS = ("undercount", "overcount", "untracked", "phantom")


def api_totals(buckets):
    """Fold usage buckets into one row per project. Pure.

    Requests are carried alongside the tokens because OpenAI reports them and
    Anthropic does not; a project with requests and no output tokens is a
    different note, and this one at least keeps the number in view.
    """
    rows = {}
    for bucket in buckets or []:
        for result in bucket.get("results") or []:
            project = str(result.get("project_id") or "unknown")
            row = rows.setdefault(project, {"tokens": 0, "requests": 0})
            row["tokens"] += (int(result.get("input_tokens") or 0)
                              + int(result.get("output_tokens") or 0))
            row["requests"] += int(result.get("num_model_requests") or 0)
    return rows


def recorded_tokens(entry):
    """Read one project's own recorded token count. Pure.

    Returns an int, or None when nothing was recorded for that project at all.
    The distinction is the point: zero means your pipeline saw the project and
    recorded nothing, None means it has never heard of it, and those are two
    different bugs with two different owners.
    """
    if entry is None:
        return None
    if isinstance(entry, bool):
        return None
    if isinstance(entry, (int, float)):
        return int(entry)
    if isinstance(entry, dict):
        if "tokens" in entry:
            try:
                return int(entry["tokens"] or 0)
            except (TypeError, ValueError):
                return None
        if "input_tokens" in entry or "output_tokens" in entry:
            try:
                return (int(entry.get("input_tokens") or 0)
                        + int(entry.get("output_tokens") or 0))
            except (TypeError, ValueError):
                return None
    return None


def compare(api_tokens, recorded, tolerance=0.05, min_tokens=100000):
    """Compare one project's two numbers. Pure. Returns (state, detail).

    Three disagreements, not one. Recorded below the API is the undercount this
    note is about. Recorded above it is double counting, a different bug that
    would be hidden by an absolute-value comparison. A project missing from the
    telemetry entirely is not undercounted, it is unrecorded.
    """
    api_tokens = int(api_tokens or 0)

    if api_tokens <= 0:
        if recorded is None or int(recorded) <= 0:
            return ("idle", "no usage in the org report and none recorded")
        return ("phantom",
                "%d token(s) recorded against a project the org report shows no "
                "usage for. That is a project id mapping, not a streaming "
                "problem." % int(recorded))

    if recorded is None:
        return ("untracked",
                "%d token(s) in the org report and no telemetry for this project "
                "at all. Not an undercount: nothing here is being recorded."
                % api_tokens)

    recorded = int(recorded)
    if api_tokens < min_tokens:
        return ("too-little-traffic",
                "%d token(s) in the window, too few for the comparison to mean "
                "anything" % api_tokens)

    gap = api_tokens - recorded
    share = gap / float(api_tokens)
    if share > tolerance:
        return ("undercount",
                "recorded %d token(s) against %d in the org report, short by %d "
                "(%.1f%%). Streamed responses report usage: null unless the "
                "request asked for the totals."
                % (recorded, api_tokens, gap, share * 100))
    if share < -tolerance:
        return ("overcount",
                "recorded %d token(s) against %d in the org report, over by %d "
                "(%.1f%%). Recording more than you were billed for is double "
                "counting, not a streaming gap."
                % (recorded, api_tokens, -gap, -share * 100))
    return ("matched",
            "recorded %d token(s) against %d in the org report (%.1f%% apart)"
            % (recorded, api_tokens, abs(share) * 100))


def untracked_cost(cost_buckets, project_id, api_tokens, gap_tokens):
    """Pro-rata dollars behind an untracked token gap. Pure.

    An estimate and nothing more: input and output are priced differently, so
    scaling a project's spend by its missing token share is only right when the
    missing traffic has the same mix as the rest. It is the right order of
    magnitude and it is read from the cost report rather than a price table,
    which is the most this can honestly claim.
    """
    api_tokens = int(api_tokens or 0)
    gap_tokens = int(gap_tokens or 0)
    if api_tokens <= 0 or gap_tokens <= 0:
        return 0.0
    spend = 0.0
    for bucket in cost_buckets or []:
        for result in bucket.get("results") or []:
            if str(result.get("project_id") or "") != str(project_id):
                continue
            try:
                spend += float((result.get("amount") or {}).get("value") or 0.0)
            except (TypeError, ValueError):
                continue
    return round(spend * min(1.0, gap_tokens / float(api_tokens)), 2)


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an "
                         "organization admin key (sk-admin-), not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params, max_pages=40):
    """Walk a usage or cost report, which paginates on an opaque page cursor."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, path, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params = dict(params)
        params["page"] = page["next_page"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--telemetry", required=True,
                    help="JSON file of your own recorded token counts, keyed by "
                         "project id")
    ap.add_argument("--days", type=int, default=7,
                    help="days to reconcile (default 7)")
    ap.add_argument("--tolerance", type=float, default=0.05,
                    help="fractional disagreement to accept as matched "
                         "(default 0.05)")
    ap.add_argument("--min-tokens", type=int, default=100000,
                    help="ignore projects below this many tokens (default 100000)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print projects that reconcile")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_ADMIN_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key, read-only "
                  "scopes are enough)")
        return 2

    try:
        with open(args.telemetry, "r", encoding="utf-8") as fh:
            telemetry = json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("could not read %s: %s", args.telemetry, exc)
        return 2
    if not isinstance(telemetry, dict):
        log.error("%s should be a JSON object keyed by project id", args.telemetry)
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    start = int(time.time()) - args.days * 86400
    usage = list(pages(session, "/organization/usage/completions", {
        "start_time": start,
        "bucket_width": "1d",
        "limit": min(31, max(1, args.days)),
        "group_by": ["project_id"],
    }))
    costs = list(pages(session, "/organization/costs", {
        "start_time": start,
        "bucket_width": "1d",
        "limit": min(180, max(1, args.days)),
        "group_by": ["project_id"],
    }))

    rows = api_totals(usage)
    for project in telemetry:
        rows.setdefault(str(project), {"tokens": 0, "requests": 0})
    if not rows:
        log.info("no completions usage in the last %d day(s) and nothing in the "
                 "telemetry file", args.days)
        return 0

    found = 0
    for project in sorted(rows):
        api_tokens = rows[project]["tokens"]
        recorded = recorded_tokens(telemetry.get(project))
        state, detail = compare(api_tokens, recorded, args.tolerance,
                                args.min_tokens)
        line = "%-18s %s  %s" % (state, project, detail)

        if state in FINDINGS:
            found += 1
            log.warning(line)
            if state == "undercount":
                gap = api_tokens - int(recorded or 0)
                money = untracked_cost(costs, project, api_tokens, gap)
                log.warning("  about $%.2f of this project's spend over %d day(s) "
                            "is not in your own numbers", money, args.days)
                log.warning("  repair: set stream_options include_usage on every "
                            "streaming Chat Completions call and read the final "
                            "chunk, or read response.usage from the terminal "
                            "response.completed event on the Responses API. "
                            "Streams the client abandons will still lose theirs.")
            elif state == "overcount":
                log.warning("  repair: this is double counting rather than a "
                            "streaming gap. Look for retries recorded once per "
                            "attempt, or one response written by two consumers.")
            elif state == "untracked":
                log.warning("  repair: this project is absent from your "
                            "telemetry. Map the project id before treating any "
                            "of these numbers as a margin.")
            else:
                log.warning("  repair: your telemetry attributes tokens to a "
                            "project the organization report has no usage for. "
                            "Check the project id, not the streaming client.")
        elif args.show_all:
            log.info(line)

    log.info("%d project(s) reconciled, %d with a gap", len(rows), found)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
