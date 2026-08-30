"""Find stored OpenAI responses that carry a refusal nobody read.

Read only. GET /v1/responses/{response_id} for each id you supply, with a
project key set to Read Only. There is no list endpoint for /v1/responses, so
the ids come from your own records: one id per line in a file.

Structured Outputs gives a safety refusal its own content type so it does not
have to be squeezed into your schema. That is the right design and it is also
why a parser reaching straight for the text finds nothing: the refusal is not
an error, not a truncation, and not schema-shaped. The response completed.

One refusal is not a finding. A refusal rate per prompt template is, which is
why this script groups before it judges: a template that refuses one call in
three has a bad input source or a bad instruction, not bad users.

The repair is printed, never performed.
"""
import argparse
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_refusal_channel")

API = "https://api.openai.com/v1"

# States this note owns. "truncated" is a handoff: an answer that was cut short
# is a different note with a different repair.
FINDINGS = ("refused", "refused-after-partial", "stopped-by-filter")

# Below this many responses in a group, a rate is a rumour.
GROUP_FLOOR = 20


def refusals(response):
    """Every refusal carried by a stored response. Pure.

    Returns dicts with the output index and the refusal text, so a caller can
    show the reader what the model actually said rather than the fact that it
    said something. Both surfaces: the Responses API puts a refusal content
    item in output[], Chat Completions puts a string on message.refusal.
    """
    found = []
    response = response or {}
    for index, item in enumerate(response.get("output") or []):
        for content in item.get("content") or []:
            if content.get("type") == "refusal":
                found.append({"index": index,
                              "text": str(content.get("refusal") or "").strip()})
    for index, choice in enumerate(response.get("choices") or []):
        text = (choice.get("message") or {}).get("refusal")
        if text:
            found.append({"index": index, "text": str(text).strip()})
    return found


def visible_text(response):
    """The text a parser would have reached for. Pure.

    Deliberately not "the answer": on a refused turn this is empty or a partial
    preamble, and the whole bug is that the calling code treats emptiness as a
    transport problem rather than as a decision the model made.
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
    return "".join(parts).strip()


def stop_reason(response):
    """Why the response stopped, in one vocabulary. Pure. None when it did not."""
    response = response or {}
    if str(response.get("status") or "") == "incomplete":
        return str((response.get("incomplete_details") or {}).get("reason") or "unknown")
    for choice in response.get("choices") or []:
        finish = str(choice.get("finish_reason") or "")
        if finish == "length":
            return "max_output_tokens"
        if finish == "content_filter":
            return "content_filter"
    return None


def group_key(response):
    """What to count refusals against. Pure.

    A refusal rate is only interesting per prompt, so metadata wins over the
    model id. Tag your calls and this script gets sharper; do not, and it still
    works at model granularity, which is the least useful grouping that is
    still true.
    """
    response = response or {}
    metadata = response.get("metadata") or {}
    for field in ("template", "prompt_template", "prompt_id", "use_case"):
        value = metadata.get(field)
        if value:
            return str(value)
    prompt = response.get("prompt") or {}
    if prompt.get("id"):
        return "prompt:" + str(prompt["id"])
    return "model:" + str(response.get("model") or "unknown")


def classify(response):
    """Classify one stored response. Pure. Returns (state, detail).

    The distinction that costs people a day is refusal against truncation. Both
    return 200 with no usable payload. One means the model declined and the
    input is the thing to look at; the other means the model was interrupted
    and the ceiling is the thing to look at.
    """
    response = response or {}
    declined = refusals(response)
    text = visible_text(response)
    reason = stop_reason(response)

    if declined:
        said = declined[0]["text"] or "(the refusal string was empty)"
        if text:
            return ("refused-after-partial",
                    "The turn produced %d character(s) of text and then "
                    "refused: %r. A reader that concatenates output items ends "
                    "up storing the preamble as if it were the answer."
                    % (len(text), said))
        return ("refused",
                "Completed with a refusal and no answer: %r. There is nothing "
                "to parse and nothing went wrong." % said)

    if reason == "content_filter":
        return ("stopped-by-filter",
                "Incomplete because the content filter halted generation. That "
                "is the platform stopping the turn, not the model declining "
                "it, and the two are worth separating in your metrics.")
    if reason == "max_output_tokens":
        return ("truncated",
                "Incomplete because the output ceiling was reached. Nothing "
                "was refused. Read the truncation note.")
    if reason is not None:
        return ("incomplete-other",
                "Incomplete for reason %r, which is neither a refusal nor a "
                "ceiling." % reason)

    if not text:
        return ("empty-answer",
                "Completed, no refusal, and no text either. Check whether the "
                "output items are a tool call rather than a message.")
    return ("answered", "Completed with %d character(s) of text." % len(text))


def refusal_rate(rows, floor=GROUP_FLOOR):
    """Refusal rate per group. Pure. Rows are (group, state) pairs.

    Returns rate None below the floor rather than a number, because one refusal
    in one call is 100% and reporting it that way trains people to ignore the
    report. Counting is still done, so a small group grows into a real one.
    """
    totals = {}
    for group, state in rows or []:
        row = totals.setdefault(str(group), {"total": 0, "refused": 0,
                                             "filtered": 0, "rate": None})
        row["total"] += 1
        if state in ("refused", "refused-after-partial"):
            row["refused"] += 1
        elif state == "stopped-by-filter":
            row["filtered"] += 1
    for row in totals.values():
        if row["total"] >= floor:
            row["rate"] = (row["refused"] + row["filtered"]) / float(row["total"])
    return totals


def repair_lines(state):
    """The repair for one state. Pure."""
    if state in ("refused", "refused-after-partial"):
        return ["Handle refusal as a first-class branch before parsing: if any "
                "output content item has type refusal, surface the refusal text "
                "to the caller and do not attempt schema parsing at all.",
                "Never treat an empty parsed value as a transport failure. A "
                "refusal is a completed answer and retrying it unchanged spends "
                "money to be told no again.",
                "Log the refusal rate per prompt template. A spike is almost "
                "always a prompt change or a bad input source, not a change in "
                "who your users are."]
    if state == "stopped-by-filter":
        return ["Branch on incomplete_details.reason as well as on the refusal "
                "content type. A filter stop is the platform halting the turn "
                "and it needs the same caller-facing message as a refusal.",
                "Count filter stops separately from model refusals. They move "
                "for different reasons and folding them together hides both."]
    if state == "truncated":
        return ["Not a refusal. Check the output ceiling before you look at the "
                "prompt: the model was interrupted, not unwilling."]
    if state == "empty-answer":
        return ["Not a refusal either. Inspect the output item types before "
                "concluding anything: a function call is not a message."]
    return []


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
    r = session.get(API + "/responses/" + response_id, timeout=60)
    if r.status_code == 404:
        return None
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: this needs a project key that can "
                         "read stored responses" % r.status_code)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", required=True,
                    help="file of stored response ids, one per line")
    ap.add_argument("--floor", type=int, default=GROUP_FLOOR,
                    help="responses a group needs before a rate is printed")
    ap.add_argument("--show-all", action="store_true",
                    help="also print responses that were answered normally")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY, a project key set to Read Only")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    rows = []
    checked = 0
    bad = 0
    for response_id in read_ids(args.ids):
        stored = fetch_response(session, response_id)
        checked += 1
        if stored is None:
            log.warning("%-22s %s  not found. Stored responses expire, and a "
                        "response created without storage was never readable.",
                        "unreadable", response_id)
            continue
        state, detail = classify(stored)
        rows.append((group_key(stored), state))
        line = "%-22s %s  %s" % (state, response_id, detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
            for repair in repair_lines(state):
                log.warning("  repair: %s", repair)
        elif state == "answered":
            if args.show_all:
                log.info(line)
        else:
            log.info(line)
            for repair in repair_lines(state):
                log.info("  note: %s", repair)

    rates = refusal_rate(rows, args.floor)
    for group in sorted(rates):
        row = rates[group]
        if row["rate"] is None:
            log.info("%-22s %s  %d response(s), under the floor of %d so no "
                     "rate is claimed", "group", group, row["total"], args.floor)
        else:
            log.warning("%-22s %s  %.1f%% of %d response(s) refused or filtered",
                        "group", group, row["rate"] * 100, row["total"])

    log.info("%d response(s) checked, %d refused or filtered", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
