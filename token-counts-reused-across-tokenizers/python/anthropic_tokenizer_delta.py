"""Measure the token delta between two Claude models on one identical body.

Claude 4.7 and later use a newer tokenizer that produces roughly 30 percent
more tokens for the same text, and the exact increase depends on the content.
This measures the increase for your bodies instead of assuming the headline.

The only non-GET request in this section: POST /v1/messages/count_tokens. It is
documented as free, it creates no message, it generates nothing, and its rate
limit is separate from message creation. Nothing else here contacts the API.

The two calls must differ only in the model field. A ratio taken across two
bodies that drifted apart measures the harness rather than the tokenizer, so
that is asserted before either request is sent.

This is a budgeting reading, not a ceiling one. It never asks whether a payload
fits: see prompt-too-long-context-overflow for max_input_tokens and
request-too-large-413 for the byte limit.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_tokenizer_delta")

# POST to /v1/messages/count_tokens, and this is the only write-shaped call in
# the section. It creates nothing, returns no completion, and bills nothing.
COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"

# Fields that belong to message creation and are not part of counting. Sending
# them is not useful and max_tokens in particular describes output, which the
# counting endpoint has no opinion about.
GENERATION_ONLY = ("max_tokens", "temperature", "top_p", "top_k", "stream",
                   "stop_sequences", "service_tier", "metadata")

# Below this the two tokenizers are the same one and the run is a no-op.
TOLERANCE = 0.02

MEASURED = "measured: two input_tokens values from count_tokens on identical bodies"
INFERRED = "inferred: that this ratio holds for traffic these %d bodies represent"

FINDINGS = ("tokenizer-delta", "count-failed", "bodies-differ")


def count_body(body):
    """A counting body from a Messages body. Pure. Generation fields removed.

    Everything that occupies the input window is kept: system, tools,
    tool_choice, thinking and the messages themselves, including images and
    documents. Only the knobs that describe generation are dropped.
    """
    if not isinstance(body, dict):
        return {}
    return {k: v for k, v in body.items() if k not in GENERATION_ONLY}


def swap_model(body, model):
    """The same body under a different model id. Pure. One key changes."""
    out = dict(body or {})
    out["model"] = str(model)
    return out


def same_apart_from_model(left, right):
    """True when the only difference is the model field. Pure.

    The guard the whole measurement rests on. Two counts of two different
    bodies is not a tokenizer ratio, it is noise with a decimal point.
    """
    a = {k: v for k, v in (left or {}).items() if k != "model"}
    b = {k: v for k, v in (right or {}).items() if k != "model"}
    return (json.dumps(a, sort_keys=True, separators=(",", ":"))
            == json.dumps(b, sort_keys=True, separators=(",", ":")))


def ratio(base, target):
    """target / base as a float. Pure. None when the base count is unusable."""
    try:
        base = int(base)
        target = int(target)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    return target / base


def workload_ratio(rows):
    """Token-weighted ratio across the sample. Pure. None when nothing counted.

    Weighted, not averaged. A mean of per-body ratios lets a two-line fixture
    count as much as the 40k-token thread that is most of the bill.
    """
    base = sum(int(r.get("base_tokens") or 0) for r in rows or []
               if r.get("base_tokens"))
    target = sum(int(r.get("target_tokens") or 0) for r in rows or []
                 if r.get("target_tokens"))
    return ratio(base, target)


def rebaseline(budgets, r):
    """[(name, old, new)] for each declared constant. Pure. Sorted by name."""
    out = []
    if not r:
        return out
    for name in sorted(budgets or {}):
        old = int(budgets[name])
        out.append((name, old, int(round(old * r))))
    return out


def parse_budgets(raw):
    """{name: tokens} from name=tokens pairs. Pure. Bad pairs are dropped."""
    out = {}
    for item in raw or []:
        for part in str(item).split(","):
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name = name.strip()
            try:
                tokens = int(str(value).strip().replace("_", ""))
            except ValueError:
                continue
            if name and tokens > 0:
                out[name] = tokens
    return out


def verdict(rows, base_model, target_model):
    """Grade the run. Pure. Returns (state, detail)."""
    rows = list(rows or [])
    if not rows:
        return ("no-bodies", "no bodies were counted, so there is nothing to "
                             "compare")
    failed = [r for r in rows if r.get("error")]
    if len(failed) == len(rows):
        return ("count-failed",
                "every count failed: %s" % failed[0].get("error"))
    if any(r.get("mismatch") for r in rows):
        return ("bodies-differ",
                "at least one pair of bodies differed by more than the model "
                "field, so no ratio was taken for it")
    r = workload_ratio(rows)
    if r is None:
        return ("count-failed", "no usable input_tokens came back")
    counted = [x for x in rows if not x.get("error")]
    if abs(r - 1.0) < TOLERANCE:
        return ("counts-agree",
                "%s and %s count this workload within %d%% of each other, so "
                "they share a tokenizer and no constant needs re-baselining"
                % (base_model, target_model, int(TOLERANCE * 100)))
    return ("tokenizer-delta",
            "the workload counts %.3fx more tokens on %s, measured over %d "
            "body/bodies" % (r, target_model, len(counted)))


def repair_lines(state, r):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state == "tokenizer-delta":
        lines = ["re-baseline every constant above, and key any stored token "
                 "count by model as well as by text. A count with no model "
                 "attached is wrong for one of the two models and you cannot "
                 "tell which."]
        if r and r > 1:
            lines.append("expect input spend on this workload to move by about "
                         "%d%% at flat traffic, since billing follows the count "
                         "the model actually consumed." % round((r - 1) * 100))
            lines.append("prompts assembled to a fixed token budget now carry "
                         "less content than they did. Check retrieval quality "
                         "and any compaction threshold before blaming the model.")
        return lines
    if state == "bodies-differ":
        return ["the two bodies differed by more than the model field, so the "
                "ratio would have measured the harness. Count one body, swap "
                "only model, and send it twice."]
    if state == "count-failed":
        return ["read the error text above. A 400 naming the model is an id "
                "this account cannot reach; a 413 is the 32 MB byte ceiling, "
                "which is a different note."]
    if state == "counts-agree":
        return ["nothing to change here. Both ids are on the same tokenizer, "
                "so counts measured on one transfer to the other."]
    return []


def count_tokens(body, key, timeout=30):
    """One count. Returns (input_tokens, error). Free, and creates nothing."""
    headers = {"x-api-key": key,
               "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        # POST to /v1/messages/count_tokens: free, no completion, no billing.
        r = requests.post(COUNT_TOKENS_URL, headers=headers, json=body,
                          timeout=timeout)
    except requests.RequestException as exc:
        return (None, "request failed: %s" % exc)
    if r.status_code != 200:
        detail = ""
        try:
            detail = str((r.json().get("error") or {}).get("message") or "")
        except ValueError:
            detail = (r.text or "")[:160]
        return (None, "HTTP %d %s" % (r.status_code, detail))
    try:
        return (int(r.json()["input_tokens"]), None)
    except (ValueError, KeyError, TypeError):
        return (None, "no input_tokens in the response")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="base", required=True,
                    help="the model the counts were originally measured on")
    ap.add_argument("--to", dest="target", required=True,
                    help="the model you are migrating to")
    ap.add_argument("--body", action="append", default=[],
                    help="a JSON file holding one real Messages request body")
    ap.add_argument("--budget", action="append", default=[],
                    help="name=tokens, repeatable, for a constant in your code")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a workspace key. It is used only "
                  "for POST /v1/messages/count_tokens, which is free and "
                  "creates nothing")
        return 2
    if not args.body:
        log.error("pass --body at least once with a real request body. A toy "
                  "message measures a toy ratio")
        return 2

    budgets = parse_budgets(list(args.budget)
                            + [os.environ.get("ANTHROPIC_TOKEN_BUDGETS", "")])
    rows = []
    for path in args.body:
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            rows.append({"name": name, "error": "unreadable: %s" % exc})
            log.warning("%-24s unreadable: %s", name, exc)
            continue

        base_body = swap_model(count_body(raw), args.base)
        target_body = swap_model(count_body(raw), args.target)
        if not same_apart_from_model(base_body, target_body):
            rows.append({"name": name, "mismatch": True})
            log.warning("%-24s the two bodies differ by more than model", name)
            continue

        base_tokens, base_err = count_tokens(base_body, key)
        target_tokens, target_err = count_tokens(target_body, key)
        err = base_err or target_err
        if err:
            rows.append({"name": name, "error": err})
            log.warning("%-24s %s", name, err)
            continue

        r = ratio(base_tokens, target_tokens)
        rows.append({"name": name, "base_tokens": base_tokens,
                     "target_tokens": target_tokens, "ratio": r})
        log.info("%-24s %s %7d -> %s %7d   x%.3f", name, args.base,
                 base_tokens, args.target, target_tokens, r or 0.0)

    state, detail = verdict(rows, args.base, args.target)
    r = workload_ratio(rows)
    emit = log.warning if state in FINDINGS else log.info
    emit("%-20s %s", state, detail)
    if state in ("tokenizer-delta", "counts-agree"):
        emit("  %s", MEASURED)
        emit("  %s", INFERRED % len([x for x in rows if x.get("ratio")]))
    for name, old, new in rebaseline(budgets, r):
        emit("  budget %-10s %9d -> %9d tokens of the old measurement",
             name, old, new)
    if not budgets:
        emit("  no budgets declared. Pass --budget name=tokens for each token "
             "constant in your code to see it re-baselined")
    for line in repair_lines(state, r):
        emit("  repair: %s", line)

    log.info("%d finding(s)", 1 if state in FINDINGS else 0)
    return 1 if state in FINDINGS else 0


if __name__ == "__main__":
    sys.exit(main())
