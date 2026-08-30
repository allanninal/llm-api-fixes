"""Find OpenAI usage buckets that counted requests and generated nothing.

Read only. One GET against the organization usage report, which needs an
organization admin key (sk-admin-) and can be provisioned read-only, plus an
optional GET /v1/models/{id} with a project key set to Read Only.

Neither API lists individual requests, so this is a shape in the aggregate
rather than an error log: num_model_requests above zero with output_tokens at
zero is a set of calls that never reached generation, and no input tokens with
it means the request body was rejected before the prompt was read.

The repair is printed, never performed. Renaming a request field is a deploy.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_zero_output_buckets")

API = "https://api.openai.com/v1"

# The families that replaced max_tokens with max_completion_tokens and refuse
# the sampling parameters outright. Matched as whole id prefixes, because a
# substring test for "o1" or "o3" also matches ids that have nothing to do with
# reasoning and a substring test for "o" matches gpt-4o.
REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")

FINDINGS = ("parameter-rejected", "partial-rejection")


def _int(value):
    """Read a usage field as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def is_reasoning_model(model):
    """Is this id one of the families that refuse max_tokens? Pure.

    Whole-prefix matching only. gpt-4o must come back False here or the script
    prints a rename that does not apply and sends somebody to change a field
    that was never the problem.
    """
    name = str(model or "").strip().lower()
    if not name:
        return False
    for prefix in REASONING_PREFIXES:
        if name == prefix or name.startswith(prefix + "-") or name.startswith(prefix + "."):
            return True
    return False


def fold(buckets):
    """Fold usage buckets into one row per (project, model). Pure.

    The silent buckets are counted rather than summed away. "Every bucket in
    the window generated nothing" and "one bucket in twelve generated nothing"
    are a broken deploy and a half-finished rollout, and a total cannot tell
    them apart.
    """
    rows = {}
    for bucket in buckets or []:
        for result in bucket.get("results") or []:
            key = (str(result.get("project_id") or "unknown"),
                   str(result.get("model") or "unknown"))
            row = rows.setdefault(key, {"requests": 0, "input": 0, "output": 0,
                                        "buckets": 0, "silent_buckets": 0,
                                        "silent_requests": 0, "silent_input": 0})
            made = _int(result.get("num_model_requests"))
            read = _int(result.get("input_tokens"))
            wrote = _int(result.get("output_tokens"))
            row["requests"] += made
            row["input"] += read
            row["output"] += wrote
            row["buckets"] += 1
            if made > 0 and wrote == 0:
                row["silent_buckets"] += 1
                row["silent_requests"] += made
                row["silent_input"] += read
    return rows


def silent_share(row):
    """Share of a row's requests that generated no output at all. Pure.

    None when there were no requests, which is a different state from zero and
    must not be rounded into one.
    """
    requests_made = _int((row or {}).get("requests"))
    if requests_made <= 0:
        return None
    return min(1.0, _int(row.get("silent_requests")) / float(requests_made))


def classify(model, row, min_requests=50, partial_floor=0.2, total_floor=0.99):
    """Classify one (project, model) row. Pure. Returns (state, detail).

    The split that matters is on input tokens inside the silent buckets. No
    input and no output means the request body was rejected on validation.
    Input read with no output means the prompt reached the model and generation
    was blocked, which is verification or a filter and a different repair.
    """
    row = row or {}
    requests_made = _int(row.get("requests"))
    if requests_made < min_requests:
        return ("too-few-requests",
                "%d request(s) in the window, under the floor of %d. A silence "
                "this small is not evidence of anything."
                % (requests_made, min_requests))

    share = silent_share(row) or 0.0
    shape = ("%d request(s) over %d bucket(s), %d input token(s) and %d output "
             "token(s)" % (requests_made, _int(row.get("buckets")),
                           _int(row.get("input")), _int(row.get("output"))))

    if share >= total_floor:
        if _int(row.get("silent_input")) == 0:
            return ("parameter-rejected",
                    shape + ". Nothing was read and nothing was generated, so "
                    "these calls were rejected on the request body before the "
                    "prompt was processed.")
        return ("generation-blocked",
                shape + ". The prompt was read and nothing came back, which is "
                "not a refused parameter name: look at organization "
                "verification, a content filter, or an output cap of zero.")

    if share >= partial_floor:
        return ("partial-rejection",
                "%s, and %.0f%% of those requests generated nothing. Part of "
                "the fleet is still sending the old field."
                % (shape, share * 100))

    return ("generating", shape + ".")


def repair_lines(model):
    """The exact request-body repair for one model id. Pure.

    Both API surfaces, because they are not interchangeable and a wrapper that
    supports both needs the branch rather than one global replace.
    """
    if is_reasoning_model(model):
        return [
            "Chat Completions: send max_completion_tokens instead of "
            "max_tokens, and raise the number. The cap now has to absorb "
            "reasoning tokens as well as the visible answer.",
            "Responses API: the same field is called max_output_tokens.",
            "Remove temperature, top_p, presence_penalty, frequency_penalty "
            "and logprobs for this model and express the intent as a reasoning "
            "effort setting. Do not send temperature 1 explicitly; omit it.",
        ]
    return [
        "This id is not one of the reasoning families, so a refused parameter "
        "name is the less likely cause here. Read one 400 body for its code "
        "and param fields before changing anything.",
    ]


def model_verdict(status):
    """What the model lookup says about whose fault the failure is. Pure."""
    if status is None:
        return ("unchecked",
                "no project key was supplied, so the model id itself was not "
                "checked")
    if status == 200:
        return ("id-resolves",
                "the id resolves for this key, so the fault is in the request "
                "body and not in access")
    if status == 404:
        return ("id-unreachable",
                "the id does not resolve for this key. That is retirement or "
                "entitlement rather than a parameter name, and it is a "
                "different repair")
    if status in (401, 403):
        return ("check-refused",
                "the project key could not read the model list, so the id was "
                "not confirmed either way")
    return ("check-inconclusive", "the model lookup returned %d" % int(status))


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an "
                         "organization admin key (sk-admin-), not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params, max_pages=40):
    """Walk the usage report, which paginates on an opaque page cursor."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, path, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params = dict(params)
        params["page"] = page["next_page"]


def check_model(key, model):
    """One cheap GET to prove the id is reachable. Returns a status code."""
    if not key:
        return None
    try:
        r = requests.get(API + "/models/" + str(model),
                         headers={"Authorization": "Bearer " + key}, timeout=30)
    except requests.RequestException:
        return None
    return r.status_code


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=24,
                    help="hours of hourly buckets to read (default 24)")
    ap.add_argument("--min-requests", type=int, default=50,
                    help="ignore rows below this many requests (default 50)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print rows that are generating normally")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key; read-only "
                  "scopes are enough)")
        return 2
    project_key = os.environ.get("OPENAI_API_KEY")

    hours = max(1, min(int(args.hours), 168))
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + admin})

    buckets = pages(session, "/organization/usage/completions", {
        "start_time": int(time.time()) - hours * 3600,
        "bucket_width": "1h",
        "limit": hours,
        "group_by": ["model", "project_id"],
    })
    rows = fold(buckets)
    if not rows:
        log.info("no completions usage in the last %d hour(s)", hours)
        return 0

    checked = 0
    bad = 0
    for project, model in sorted(rows, key=lambda k: -rows[k]["requests"]):
        row = rows[(project, model)]
        state, detail = classify(model, row, args.min_requests)
        checked += 1
        line = "%-19s %s / %s  %s" % (state, project, model, detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            _, note = model_verdict(check_model(project_key, model))
            log.warning("  %s", note)
            for repair in repair_lines(model):
                log.warning("  repair: %s", repair)
        elif state == "generation-blocked":
            log.warning(line)
            log.warning("  repair: this is not the parameter rename. Check "
                        "organization verification for the streaming path and "
                        "the project's model permissions before touching the "
                        "request body.")
        elif args.show_all:
            log.info(line)

    log.info("%d model/project row(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
