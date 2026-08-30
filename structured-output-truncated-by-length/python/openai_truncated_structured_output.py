"""Find stored OpenAI responses whose structured output was cut off mid-object.

Read only. GET /v1/responses/{response_id} for each id you supply, with a
project key set to Read Only, and optionally one GET against an Anthropic
Message Batches results file, which is a complete corpus of finished responses
and needs a workspace key.

There is no list endpoint for /v1/responses, so the ids have to come from your
own records: one id per line in a file. That is a limitation of the API and not
of this script.

The finding is a request that succeeded and stopped early: status "incomplete"
with an incomplete_details reason of max_output_tokens on the Responses API,
stop_reason "max_tokens" in an Anthropic batch result. The body is a valid
prefix of the answer, and a prefix of valid JSON is not JSON.

The repair is printed, never performed. Raising a ceiling or reshaping a schema
is a deploy with an owner.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_truncated_structured_output")

OPENAI_API = "https://api.openai.com/v1"
ANTHROPIC_API = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# The states this note owns. Everything else the classifier can return is a
# handoff to a sibling note and is reported without being counted as a finding.
FINDINGS = ("truncated-by-length", "ceiling-spent-on-reasoning", "cut-without-a-reason")

# Above this share of the output tokens, the ceiling was consumed by reasoning
# before the visible answer began. That is a different repair from a schema
# that is simply too large, so it gets its own state.
REASONING_DOMINANT = 0.6


def output_text(response):
    """Concatenate the visible text of a stored response. Pure.

    Both surfaces, because a codebase that has half-migrated to the Responses
    API stores both shapes and a checker that only reads one of them reports
    every Chat Completions record as empty.
    """
    parts = []
    response = response or {}
    for item in response.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in ("output_text", "text"):
                parts.append(str(content.get("text") or ""))
    for choice in response.get("choices") or []:
        text = (choice.get("message") or {}).get("content")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def json_state(text):
    """Where a JSON document stops. Pure. One of empty, parses, truncated,
    malformed.

    "truncated" means the text is a valid prefix that never closes: a string
    still open, or a brace still owed. That is the difference between an answer
    that was cut and an answer the model got wrong, and json.loads collapses
    both into one exception with the same message.
    """
    body = str(text or "").strip()
    if not body:
        return "empty"
    try:
        json.loads(body)
        return "parses"
    except ValueError:
        pass

    depth = 0
    in_string = False
    escaped = False
    for ch in body:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth < 0:
                return "malformed"
    if in_string or escaped or depth > 0:
        return "truncated"
    return "malformed"


def incomplete_reason(response):
    """Why a stored response stopped early, or None. Pure.

    The Responses API says it in status plus incomplete_details.reason. Chat
    Completions said it in finish_reason, and the two vocabularies are mapped
    onto one here so the rest of the script has a single word to branch on.
    """
    response = response or {}
    if str(response.get("status") or "") == "incomplete":
        details = response.get("incomplete_details") or {}
        return str(details.get("reason") or "unknown")
    for choice in response.get("choices") or []:
        finish = str(choice.get("finish_reason") or "")
        if finish == "length":
            return "max_output_tokens"
        if finish == "content_filter":
            return "content_filter"
    return None


def has_refusal(response):
    """Does this response carry a refusal rather than an answer? Pure."""
    response = response or {}
    for item in response.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") == "refusal":
                return True
    for choice in response.get("choices") or []:
        if (choice.get("message") or {}).get("refusal"):
            return True
    return False


def ceiling_use(response):
    """Output tokens as a share of the configured ceiling. Pure.

    None when the response does not carry a ceiling, which is a different state
    from zero and must not be printed as one.
    """
    response = response or {}
    usage = response.get("usage") or {}
    try:
        cap = int(response.get("max_output_tokens"))
        used = int(usage.get("output_tokens"))
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    return min(1.0, used / float(cap))


def reasoning_share(response):
    """Share of the output tokens that were never returned to you. Pure.

    Reasoning tokens sit inside the same ceiling as the visible answer, so a
    cap sized for the old model can be entirely consumed before generation of
    the JSON starts. None when the response does not report them.
    """
    usage = (response or {}).get("usage") or {}
    details = usage.get("output_tokens_details") or {}
    try:
        total = int(usage.get("output_tokens"))
        reasoning = int(details.get("reasoning_tokens"))
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return min(1.0, reasoning / float(total))


def classify(response):
    """Classify one stored response. Pure. Returns (state, detail).

    Four of the states are handoffs. Two notes in this batch read the same
    object and reach a different conclusion from it, and a script that folded
    them in here would be telling a reader to raise a ceiling that was never
    reached.
    """
    response = response or {}
    reason = incomplete_reason(response)
    text = output_text(response)
    shape = json_state(text)
    used = ceiling_use(response)
    at_cap = "" if used is None else " Output sat at %.0f%% of the configured ceiling." % (used * 100)

    if reason == "max_output_tokens":
        thinking = reasoning_share(response)
        if thinking is not None and thinking >= REASONING_DOMINANT:
            return ("ceiling-spent-on-reasoning",
                    "Stopped on the output ceiling with %.0f%% of the output "
                    "tokens spent on reasoning, so the visible answer barely "
                    "started.%s" % (thinking * 100, at_cap))
        if shape == "truncated":
            return ("truncated-by-length",
                    "Stopped on the output ceiling mid-object: the text is a "
                    "valid prefix that never closes.%s" % at_cap)
        return ("truncated-by-length",
                "Stopped on the output ceiling. The stored text is %s.%s"
                % (shape, at_cap))

    if reason == "content_filter":
        return ("stopped-by-filter",
                "Generation was halted by the content filter rather than by "
                "the ceiling. That is the refusal note, not this one.")
    if reason is not None:
        return ("incomplete-other",
                "Incomplete for reason %r, which is not an output ceiling." % reason)

    if has_refusal(response):
        return ("refused",
                "The response completed and carries a refusal instead of an "
                "answer. Nothing was cut. Read the refusal note.")

    if shape == "parses":
        return ("complete", "Completed and the stored text parses.")
    if shape == "empty":
        return ("empty-output",
                "Completed with no text at all, which a ceiling reached during "
                "reasoning can also produce without reporting one.")
    if shape == "truncated":
        return ("cut-without-a-reason",
                "The text stops mid-object and the response reports no reason "
                "for it. Read the raw record: a Chat Completions row stored "
                "without its finish_reason looks exactly like this.")
    return ("schema-not-followed",
            "Completed, and the text is broken in a way truncation does not "
            "explain. That is an advisory schema, not a ceiling.")


def repair_lines(state, response):
    """The repair for one state, with the numbers from this response. Pure."""
    usage = (response or {}).get("usage") or {}
    cap = (response or {}).get("max_output_tokens")
    used = usage.get("output_tokens")

    if state == "truncated-by-length":
        lines = ["Check that the response completed before parsing anything: "
                 "branch on status and on incomplete_details.reason, and never "
                 "hand the text to a JSON parser until it says completed."]
        if cap and used:
            lines.append("This call was capped at %s output tokens and used %s "
                         "of them. Raise the ceiling above the largest record "
                         "the schema can emit, with room for reasoning."
                         % (cap, used))
        else:
            lines.append("Raise the output ceiling above the largest record "
                         "the schema can emit, with room for reasoning.")
        lines.append("Or reshape the schema so one call emits fewer and "
                     "shorter fields, and paginate. A long free-text field or "
                     "an unbounded array inside the schema is the usual cause.")
        return lines

    if state == "ceiling-spent-on-reasoning":
        return ["The ceiling covers reasoning tokens as well as the answer, and "
                "here it was gone before the JSON began. Raise it, or lower the "
                "reasoning effort for this call.",
                "A structured-output call that needs no deliberation is the "
                "cheapest place to spend less thinking."]

    if state == "cut-without-a-reason":
        return ["Store the whole response object, not just its text. Without "
                "status, incomplete_details and usage there is no way to tell a "
                "cut answer from a wrong one after the fact."]

    if state == "stopped-by-filter":
        return ["Not a ceiling. Handle the filter stop and the refusal channel "
                "together, as a first-class branch before parsing."]
    if state == "refused":
        return ["Not a ceiling. Read the refusal text and surface it; a refusal "
                "is an answer, not an error and not a truncation."]
    if state == "schema-not-followed":
        return ["Not a ceiling. Check whether strict was set on the schema at "
                "all, because an advisory schema produces exactly this."]
    return []


def batch_line_verdict(line):
    """Read one line of an Anthropic batch results file. Pure.

    Returns (custom_id, state, detail). Results arrive in any order, so the
    custom_id is the only safe key; position is meaningless.
    """
    try:
        record = json.loads(str(line or ""))
    except ValueError:
        return (None, "unreadable", "the line is not JSON")
    if not isinstance(record, dict):
        return (None, "unreadable", "the line is not an object")

    custom_id = record.get("custom_id")
    result = record.get("result") or {}
    if str(result.get("type") or "") != "succeeded":
        return (custom_id, "not-succeeded",
                "result type %r, which is a different note"
                % str(result.get("type") or "missing"))

    message = result.get("message") or {}
    stop = str(message.get("stop_reason") or "")
    blocks = message.get("content") or []
    last = (blocks[-1] or {}).get("type") if blocks else None
    if stop == "max_tokens":
        if last == "tool_use":
            return (custom_id, "truncated-tool-use",
                    "cut on the ceiling and the final block is an incomplete "
                    "tool_use, so the arguments cannot be executed at all")
        return (custom_id, "truncated-by-length",
                "cut on the ceiling with %s output token(s)"
                % ((message.get("usage") or {}).get("output_tokens", "an unknown number of")))
    return (custom_id, "complete", "stop_reason %r" % (stop or "missing"))


def read_ids(path):
    """One response id per line, blanks and # comments ignored."""
    ids = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if line and not line.startswith("#"):
                ids.append(line)
    return ids


def fetch_response(session, response_id):
    r = session.get(OPENAI_API + "/responses/" + response_id, timeout=60)
    if r.status_code == 404:
        return None
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: this needs a project key that can "
                         "read stored responses" % r.status_code)
    r.raise_for_status()
    return r.json()


def fetch_batch_results(key, batch_id):
    """Stream an Anthropic batch results file, one JSONL line at a time."""
    url = ANTHROPIC_API + "/messages/batches/" + batch_id + "/results"
    with requests.get(url, headers={"x-api-key": key,
                                    "anthropic-version": ANTHROPIC_VERSION},
                      stream=True, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if line:
                yield line


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", help="file of stored response ids, one per line")
    ap.add_argument("--batch", help="Anthropic message batch id to read results for")
    ap.add_argument("--show-all", action="store_true",
                    help="also print responses that completed cleanly")
    args = ap.parse_args()

    if not args.ids and not args.batch:
        log.error("give --ids (a file of stored response ids) or --batch "
                  "(an Anthropic batch id), or both")
        return 2

    checked = 0
    bad = 0

    if args.ids:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            log.error("set OPENAI_API_KEY, a project key set to Read Only")
            return 2
        session = requests.Session()
        session.headers.update({"Authorization": "Bearer " + key})
        for response_id in read_ids(args.ids):
            stored = fetch_response(session, response_id)
            checked += 1
            if stored is None:
                log.warning("%-26s %s  not found. Stored responses expire, and "
                            "a response created without storage was never "
                            "readable.", "unreadable", response_id)
                continue
            state, detail = classify(stored)
            line = "%-26s %s  %s" % (state, response_id, detail)
            if state in FINDINGS:
                bad += 1
                log.warning(line)
                for repair in repair_lines(state, stored):
                    log.warning("  repair: %s", repair)
            elif state in ("complete",):
                if args.show_all:
                    log.info(line)
            else:
                log.info(line)
                for repair in repair_lines(state, stored):
                    log.info("  note: %s", repair)

    if args.batch:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            log.error("set ANTHROPIC_API_KEY, a workspace key, to read batch results")
            return 2
        counts = {}
        for line in fetch_batch_results(key, args.batch):
            custom_id, state, detail = batch_line_verdict(line)
            checked += 1
            counts[state] = counts.get(state, 0) + 1
            if state in ("truncated-by-length", "truncated-tool-use"):
                bad += 1
                log.warning("%-26s %s  %s", state, custom_id, detail)
        for state in sorted(counts):
            log.info("batch %s: %d line(s) %s", args.batch, counts[state], state)

    log.info("%d response(s) checked, %d cut short", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
