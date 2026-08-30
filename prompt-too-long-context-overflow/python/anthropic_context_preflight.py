"""Pre-flight a Claude payload against the model's context window.

Read only, with one deliberate exception. Nothing here creates a completion:
the payload goes to /v1/messages/count_tokens, which is free, generates no
output, creates no object and bills nothing. It returns an input_tokens number
and runs against its own rate limit. That is the only way to learn what a body
costs in tokens without paying for an answer, so it is the one non-GET call in
this script. Everything else is a GET, and /v1/messages is never called.

The repair is printed, never applied. Deciding which half of a conversation to
drop is a product decision, not the side effect of an audit.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_context_preflight")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# count_tokens takes the same structured body as message creation minus the
# parameters that only mean something when text is actually generated. Sending
# max_tokens to it is a 400, which is a confusing way for a pre-flight to fail,
# so these are stripped by name rather than hoped over.
SAMPLING_ONLY = ("max_tokens", "stream", "temperature", "top_p", "top_k",
                 "stop_sequences", "metadata", "service_tier")

OVERFLOW_STOP = "model_context_window_exceeded"
TOO_LONG = "prompt is too long"

FINDINGS = ("input-over-window", "budget-over-window", "window-tight")


def count_body(body):
    """The subset of a Messages body the counting endpoint accepts. Pure.

    Everything structural stays: system, messages, tools, tool_choice, thinking.
    All of it occupies the window, so dropping any of it to make the count
    simpler would produce a number about a request you are not sending.
    """
    if not isinstance(body, dict):
        return {}
    return {k: v for k, v in body.items() if k not in SAMPLING_ONLY}


def window_of(model_obj):
    """max_input_tokens off a model object, or None. Pure.

    None is not a large window. The field is returned by the API, but a proxy
    or gateway that reshapes the model object can drop it, and a ceiling that
    went missing has to stay missing rather than defaulting to something
    generous enough to let every payload pass.
    """
    if not isinstance(model_obj, dict):
        return None
    value = model_obj.get("max_input_tokens")
    return value if isinstance(value, int) and value > 0 else None


def budget(counted_input, max_tokens):
    """What one request reserves in the window. Pure.

    Input plus the room set aside for output, because max_tokens occupies the
    window whether or not the model uses it. Checking input alone is the common
    version of this check and it passes requests that are going to fail.
    """
    return int(counted_input or 0) + max(0, int(max_tokens or 0))


def verdict(counted_input, max_tokens, window, tight=0.9):
    """Classify one payload against one model's window. Pure. (state, detail).

    Two overflow states rather than one, because they do not fail alike: over
    on input alone is a 400 on every model, and over on the reservation is a
    200 on Claude 4.5 and newer that stops with model_context_window_exceeded.
    """
    counted_input = int(counted_input or 0)
    reserved = budget(counted_input, max_tokens)

    if window is None:
        return ("window-unknown",
                "%d input token(s) counted, and the model object carried no "
                "max_input_tokens, so there is no ceiling to compare against"
                % counted_input)

    shape = ("%d input + %d max_tokens = %d of a %d token window"
             % (counted_input, max(0, int(max_tokens or 0)), reserved, window))

    if counted_input > window:
        return ("input-over-window",
                "%s. The input alone is over the window, so this 400s with "
                "prompt is too long on every model, before max_tokens is even "
                "considered." % shape)
    if reserved > window:
        return ("budget-over-window",
                "%s. The input fits and the reservation does not. On Claude 4.5 "
                "and newer that returns 200 with stop_reason %s, which a client "
                "checking only for end_turn files as a complete answer."
                % (shape, OVERFLOW_STOP))

    share = reserved / float(window)
    if share >= tight:
        return ("window-tight",
                "%s (%.0f%%). It fits today and one longer turn ends that."
                % (shape, share * 100))
    return ("fits", "%s (%.0f%%)." % (shape, share * 100))


def turns_remaining(counted_input, max_tokens, window, per_turn):
    """How many more turns of `per_turn` tokens fit. Pure. None if unanswerable.

    A conversational product's real question is not whether this payload fits
    but how many exchanges are left before one stops fitting, and that is the
    number that turns an overflow into a scheduled piece of work.
    """
    if not window or not per_turn or per_turn <= 0:
        return None
    room = window - budget(counted_input, max_tokens)
    return max(0, int(room // per_turn))


def batch_overflows(lines):
    """Find window overflows in a batch results stream. Pure.

    Both shapes, because the same fault wears two faces. A succeeded result
    carrying stop_reason model_context_window_exceeded is the 200 nobody
    noticed; an errored result whose message says the prompt is too long is the
    400. Keyed by custom_id and never by position: results arrive in any order.
    """
    out = {}
    for line in lines or []:
        record = line
        if isinstance(record, (str, bytes)):
            text = record.decode("utf-8") if isinstance(record, bytes) else record
            text = text.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                continue
        if not isinstance(record, dict):
            continue

        custom_id = record.get("custom_id")
        result = record.get("result") or {}
        message = result.get("message") or {}
        if message.get("stop_reason") == OVERFLOW_STOP:
            out[custom_id] = "truncated-with-200"
            continue
        error = result.get("error") or {}
        if TOO_LONG in str(error.get("message") or "").lower():
            out[custom_id] = "rejected-with-400"
    return out


def get(session, path):
    """Every model and batch read in this script. GET only."""
    r = session.get(API + path, timeout=30)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: ANTHROPIC_API_KEY has to be a "
                         "workspace key that can reach /v1/models" % r.status_code)
    r.raise_for_status()
    return r.json()


def count_tokens(session, body):
    """The one call here that is not a GET, and it is not a write either.

    /v1/messages/count_tokens creates no object, generates no completion and
    is not billed. It carries its own rate limit, so a pre-flight on every
    request does not eat into the message limiter. A 413 back from it means the
    body is over the 32 MB byte ceiling, which is a different problem with a
    different note.
    """
    r = session.post(API + "/messages/count_tokens",
                     json=count_body(body), timeout=60)
    if r.status_code == 413:
        raise SystemExit("413 from the counting endpoint: this body is over the "
                         "32 MB request ceiling, which is a byte problem rather "
                         "than a token one")
    r.raise_for_status()
    return int((r.json() or {}).get("input_tokens") or 0)


def batch_results(session, batch_id):
    """Stream one batch's results file. GET, and read as lines."""
    r = session.get(API + "/messages/batches/" + str(batch_id) + "/results",
                    timeout=120, stream=True)
    r.raise_for_status()
    return list(r.iter_lines(decode_unicode=True))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--payload", action="append", default=[], metavar="FILE",
                    help="a JSON file holding a real Messages request body")
    ap.add_argument("--batch-id", action="append", default=[],
                    help="also scan a finished batch's results for overflows")
    ap.add_argument("--per-turn", type=int, default=0,
                    help="average tokens one conversational turn adds, used to "
                         "report how many turns of headroom are left")
    ap.add_argument("--tight", type=float, default=0.9,
                    help="share of the window above which a payload that still "
                         "fits is reported anyway (default 0.9)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print payloads with plenty of window left")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a workspace key")
        return 2
    if not args.payload and not args.batch_id:
        log.error("give at least one --payload FILE or --batch-id ID")
        return 2

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION,
                            "content-type": "application/json"})

    windows = {}
    checked = 0
    bad = 0

    for path in args.payload:
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
        model = str(body.get("model") or "")
        if not model:
            bad += 1
            log.warning("%-20s %-30s no model field, so there is no window to "
                        "check it against", "no-model", path)
            continue
        if model not in windows:
            windows[model] = window_of(get(session, "/models/" + model))

        counted = count_tokens(session, body)
        state, detail = verdict(counted, body.get("max_tokens"),
                                windows[model], args.tight)
        checked += 1
        line = "%-20s %-30s %s" % (state, path, detail)
        if state in FINDINGS or state == "window-unknown":
            if state in FINDINGS:
                bad += 1
            log.warning(line)
        elif args.show_all:
            log.info(line)

        left = turns_remaining(counted, body.get("max_tokens"),
                               windows[model], args.per_turn)
        if left is not None:
            log.info("  room for %d more turn(s) at %d tokens each",
                     left, args.per_turn)
        if state in FINDINGS:
            log.warning("  repair: server side compaction (compact-2026-01-12) "
                        "for long conversations, context editing "
                        "(clear_tool_uses_20250919 / clear_thinking_20251015) "
                        "for agent loops, or the tool search tool so tool "
                        "definitions stop being resident on every turn")
            log.warning("  repair: caching does not help here. Cached tokens "
                        "still occupy the window; they only cost less.")

    for batch_id in args.batch_id:
        found = batch_overflows(batch_results(session, batch_id))
        checked += len(found)
        for custom_id, shape in sorted(found.items(), key=lambda kv: str(kv[0])):
            bad += 1
            log.warning("%-20s %-30s in batch %s", shape, custom_id, batch_id)

    log.info("%d payload(s) and batch result(s) checked, %d finding(s)",
             checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
