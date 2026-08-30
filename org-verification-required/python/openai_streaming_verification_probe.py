"""Find a model that one key can list and another key cannot generate with.

Read only. Two GET endpoints: /v1/organization/usage/completions with an admin
read key, and /v1/models/{id} with a Read Only project key. No request body is
ever constructed, and nothing here sends a completion of any kind.

The subject is a contrast, not a row. Requests counted with no tokens either
side means calls rejected before generation, which is the signature of several
different faults. A fault that lives in the model refuses every key; a gate on
one route does not. So this folds usage by api_key_id inside a model and looks
for two keys that disagree.

What cannot be read is stated rather than guessed: no OpenAI endpoint reports
whether an organization is verified, and the 400 body that would name it is not
retrievable after the fact. The script separates the measurement from the
inference in its own output.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_streaming_verification_probe")

API = "https://api.openai.com/v1"

# Only one state here is this note's finding. The others are real states that
# belong to other notes, and they are printed with the other note's name rather
# than folded into a verdict this script has no evidence for.
FINDINGS = ("verification-suspected",)

MEASURED = ("requests on one key were rejected before generation, on a model "
            "another key is generating with normally")
INFERRED = ("organization verification, which gates streaming and reasoning "
            "summaries. No endpoint reports verification state, so this is the "
            "most likely cause and not a reading")


def flatten(buckets):
    """[(model, api_key_id, requests, input_tokens, output_tokens)]. Pure.

    Every count is coerced. A missing field becomes 0 rather than None, because
    the whole note is a comparison between two numbers and a None propagating
    into it would read as silence from a key that was merely unreported.
    """
    rows = []
    for bucket in buckets or []:
        for entry in (bucket or {}).get("results") or []:
            row = entry or {}
            counts = []
            for field in ("num_model_requests", "input_tokens", "output_tokens"):
                try:
                    counts.append(int(row.get(field) or 0))
                except (TypeError, ValueError):
                    counts.append(0)
            rows.append((str(row.get("model") or "(unattributed)"),
                         str(row.get("api_key_id") or "(unattributed)"),
                         counts[0], counts[1], counts[2]))
    return rows


def by_model(rows):
    """{model: {api_key_id: {requests, input, output}}}. Pure.

    Summed, because a key appears once per hourly bucket and the question is
    about a whole window rather than about any one hour in it.
    """
    out = {}
    for model, key_id, requests_n, input_n, output_n in rows or []:
        slot = out.setdefault(model, {}).setdefault(
            key_id, {"requests": 0, "input": 0, "output": 0})
        slot["requests"] += requests_n
        slot["input"] += input_n
        slot["output"] += output_n
    return out


def key_state(row, min_requests=1):
    """What one key did on one model. Pure. One of four words.

    "mute" and "no-output" are deliberately not the same word. A request that
    was rejected before generation bills nothing at all; a request that read
    input and produced nothing ran and stopped, which is a truncation or
    content question with an entirely different repair.
    """
    row = row or {}
    requests_n = int(row.get("requests") or 0)
    if requests_n < max(1, int(min_requests)):
        return "idle"
    if int(row.get("output") or 0) > 0:
        return "producing"
    if int(row.get("input") or 0) > 0:
        return "no-output"
    return "mute"


def contrast(per_key, min_requests=1):
    """The note itself. Pure. Returns (state, detail).

    Looks for disagreement between keys and nothing else. No threshold, no
    ratio: one mute key beside one producing key on the same model in the same
    window is the entire claim, and any other combination is somebody else's.
    """
    per_key = dict(per_key or {})
    states = {k: key_state(v, min_requests) for k, v in per_key.items()}
    mute = sorted(k for k, s in states.items() if s == "mute")
    producing = sorted(k for k, s in states.items() if s == "producing")
    silent = sorted(k for k, s in states.items() if s == "no-output")
    active = mute + producing + silent

    if not active:
        return ("no-traffic", "no key sent enough requests to grade")
    if mute and producing:
        first_mute = per_key[mute[0]]
        first_prod = per_key[producing[0]]
        return ("verification-suspected",
                "%s billed %s request(s) with no tokens either side while %s "
                "produced %s output token(s) on the same model in the same "
                "window" % (mute[0], format(first_mute["requests"], ","),
                            producing[0], format(first_prod["output"], ",")))
    if mute and len(active) == 1:
        return ("single-key-model",
                "%s is the only key with traffic on this model, so there is "
                "nothing to compare it against" % mute[0])
    if mute:
        return ("model-wide-mute",
                "all %d key(s) with traffic are mute, so this is a property of "
                "the model or the body every caller sends" % len(active))
    if silent and not producing:
        return ("input-without-output",
                "%d key(s) consumed input and produced no output, which is a "
                "request that ran rather than one that was refused"
                % len(silent))
    return ("healthy",
            "%d key(s) with traffic, all producing output" % len(producing))


def verdict(model_status, per_key, min_requests=1):
    """Combine reachability with the contrast. Pure. Returns (state, detail).

    The lookup can veto. A model that does not resolve for a project key is a
    question about the model list, which is another note entirely, and grading
    a usage contrast on top of it would attach the wrong repair to it.
    """
    state, detail = contrast(per_key, min_requests)
    if model_status is None:
        return (state, detail + " (the model id itself was not checked, so "
                                "supply a project key to rule out access)")
    status = int(model_status)
    if status == 404:
        return ("model-not-visible",
                "the id does not resolve for the project key. That is "
                "retirement or entitlement rather than a gated feature, and it "
                "belongs to the model-list note")
    if status in (401, 403):
        return (state, detail + " (the model lookup was refused, so access was "
                                "not confirmed either way)")
    if status != 200:
        return (state, detail + " (the model lookup returned %d)" % status)
    return (state, detail)


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed.

    The measured line and the inferred line are always printed together for the
    finding, because the difference between them is the difference between what
    this script saw and what it thinks it means.
    """
    if state == "verification-suspected":
        return [
            "measured: " + MEASURED,
            "inferred: " + INFERRED,
            "verify the organization in Console, then allow up to 15 minutes "
            "to propagate. One government ID verifies one organization per 90 "
            "days, which matters if several organizations share an owner.",
            "as a stopgap on the affected route only, unset stream and buffer "
            "the whole response, and remove reasoning summary requests. Leave "
            "the batch and evaluation routes alone; they are already working.",
            "if the organization is already verified, the next candidate is a "
            "parameter that route sends and the working key does not. Diff the "
            "two request builders before changing anything in Console.",
        ]
    if state == "model-wide-mute":
        return ["not this note. Read the reasoning-model parameter note: "
                "max_tokens, temperature and top_p are refused by name on "
                "those families, and a refusal by name hits every key."]
    if state == "single-key-model":
        return ["route a canary through a second key on the same model, or "
                "read the verification setting in Console. With one key there "
                "is no contrast, and this script will not invent one.",
                "measured: requests were rejected before generation on the "
                "only key that uses this model. Nothing more than that."]
    if state == "model-not-visible":
        return ["check the id against GET /v1/models first. A model that does "
                "not resolve is a retirement or entitlement question, and it "
                "has a different repair from a gated capability."]
    if state == "input-without-output":
        return ["these requests reached the model and returned nothing, which "
                "is truncation or a refusal rather than a rejected body. Read "
                "the structured-output and refusal notes instead."]
    return []


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
    ap.add_argument("--min-requests", type=int, default=20,
                    help="requests below which a key is treated as idle")
    ap.add_argument("--model", action="append", default=[],
                    help="restrict to these model ids (repeatable)")
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
        "group_by": ["model", "api_key_id"],
    })
    grouped = by_model(flatten(buckets))
    wanted = set(args.model or [])
    if wanted:
        grouped = {m: v for m, v in grouped.items() if m in wanted}
    if not grouped:
        log.info("no completions usage in the last %d hour(s)", hours)
        return 0

    log.info("%d model(s) with traffic in the last %dh", len(grouped), hours)
    findings = 0

    for model in sorted(grouped, key=lambda m: -sum(
            r["requests"] for r in grouped[m].values())):
        per_key = grouped[model]
        preliminary, _ = contrast(per_key, args.min_requests)
        status = (check_model(project_key, model)
                  if preliminary in ("verification-suspected",
                                     "single-key-model", "model-wide-mute")
                  else None)
        state, detail = verdict(status, per_key, args.min_requests)

        emit = log.warning if state in FINDINGS or state != "healthy" else log.info
        emit("%-23s %s: %s", state, model, detail)
        if status is not None:
            emit("  model lookup: %d", int(status))
        for line in repair_lines(state):
            emit("  repair: %s" if not line.startswith(("measured:", "inferred:"))
                 else "  %s", line)
        if state in FINDINGS:
            findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
