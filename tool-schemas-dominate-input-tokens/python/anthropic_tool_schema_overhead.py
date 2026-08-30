"""Measure what a Claude tools block costs in input tokens on every call.

Read only. One GET for the model object and a handful of calls to
/v1/messages/count_tokens, which is free, creates no object, generates no
completion and is not billed. /v1/messages is never called.

The method is subtraction: count the exact body, count it again with tools
removed, and the difference is the per-call tool overhead. Ablating one tool at
a time prices each schema, and the deltas deliberately do not sum to the whole,
because every ablated body still carries the automatic tool-use system prompt.

The repair is printed, never performed. A cache breakpoint is a deploy.
"""
import argparse
import copy
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_tool_schema_overhead")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# Fields the counting endpoint refuses. Stripped from every body before it is
# counted, and stripped identically from all of them so the subtraction stays
# honest: a field removed from one body and not another moves the difference.
SAMPLING_ONLY = ("max_tokens", "stream", "temperature", "top_p", "top_k",
                 "stop_sequences", "metadata", "service_tier")

# The automatic tool-use system prompt, per model, as (auto_or_none,
# any_or_tool). Added by the API whenever any tool is present, so it is part of
# the overhead and no amount of pruning removes it. Matched on longest prefix:
# a substring test reads claude-opus-4-5 as claude-opus-5 and reports the wrong
# fixed charge with total confidence.
TOOL_SYSTEM_PROMPT = {
    "claude-opus-5": (286, 406),
    "claude-opus-4-8": (290, 410),
    "claude-opus-4-7": (675, 804),
    "claude-opus-4-6": (497, 589),
    "claude-sonnet-4-6": (497, 589),
    "claude-sonnet-5": (354, 474),
    "claude-opus-4-5": (496, 588),
    "claude-sonnet-4-5": (496, 588),
    "claude-haiku-4-5": (496, 588),
}

FINDINGS = ("schema-dominates", "schema-heavy")


def _int(value):
    """Read a token count as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def countable(body):
    """A body the counting endpoint will accept. Pure. Does not mutate.

    Only the sampling fields go. Everything being measured stays, because the
    number is worthless if the thing counted is not the thing sent.
    """
    if not isinstance(body, dict):
        return {}
    return {k: copy.deepcopy(v) for k, v in body.items() if k not in SAMPLING_ONLY}


def without_tools(body):
    """The same body with the whole tools block removed. Pure.

    tool_choice goes with it. A body that names a tool it no longer declares is
    rejected, and the rejection would be read as "the counter is broken".
    """
    stripped = countable(body)
    stripped.pop("tools", None)
    stripped.pop("tool_choice", None)
    return stripped


def tool_names(body):
    """Named tools in a body, in declaration order. Pure."""
    out = []
    for tool in (body or {}).get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def without_tool(body, name):
    """The same body with exactly one tool removed. Pure. Does not mutate."""
    stripped = countable(body)
    kept = [t for t in stripped.get("tools") or []
            if not (isinstance(t, dict) and str(t.get("name") or "") == str(name))]
    stripped["tools"] = kept
    if not kept:
        stripped.pop("tools", None)
        stripped.pop("tool_choice", None)
    return stripped


def overhead(total, base):
    """Tokens attributable to the tools block. Pure. Never negative."""
    return max(0, _int(total) - _int(base))


def overhead_share(total, base):
    """Share of the counted input that the tools block accounts for. Pure.

    None when nothing was counted, which is a different state from zero and
    must not be printed as 0%.
    """
    counted = _int(total)
    if counted <= 0:
        return None
    return overhead(total, base) / float(counted)


def choice_kind(body):
    """Which column of the tool-use system prompt table applies. Pure.

    auto and none share one size; any and a named tool share the larger one.
    """
    choice = (body or {}).get("tool_choice")
    kind = ""
    if isinstance(choice, str):
        kind = choice.strip().lower()
    elif isinstance(choice, dict):
        kind = str(choice.get("type") or "").strip().lower()
    if kind in ("any", "tool"):
        return "any"
    return "auto"


def system_prompt_tokens(model, kind="auto"):
    """The automatic tool-use system prompt for one model. Pure. None if unlisted.

    Longest prefix wins. Unlisted ids return None rather than a neighbour's
    number, because a plausible wrong number here silently corrupts the split
    between "your schemas" and "the fixed charge".
    """
    name = str(model or "").strip().lower()
    best = None
    best_len = -1
    for prefix, sizes in TOOL_SYSTEM_PROMPT.items():
        if (name == prefix or name.startswith(prefix + "-")) and len(prefix) > best_len:
            best = sizes
            best_len = len(prefix)
    if best is None:
        return None
    return best[1] if str(kind).lower() == "any" else best[0]


def fixed_overhead(total_overhead, per_tool):
    """The part of the tool overhead that belongs to no single tool. Pure.

    Ablating one tool never removes the automatic tool-use system prompt,
    because the remaining tools still require it. So the per-tool deltas sum to
    the schema weight alone and the residual is the fixed charge for having any
    tools at all. Printing the sum as if it were the total is the mistake this
    function exists to make impossible.
    """
    measured = sum(max(0, _int(row.get("tokens"))) for row in per_tool or [])
    return max(0, _int(total_overhead) - measured), measured


def classify(total, base, dominate=0.5, heavy=0.25):
    """Classify one measured payload. Pure. Returns (state, detail)."""
    counted = _int(total)
    if counted <= 0:
        return ("nothing-counted",
                "the counting endpoint returned no tokens for this body")
    weight = overhead(total, base)
    if weight <= 0:
        return ("no-tools",
                "%d input token(s) and no measurable tools block" % counted)
    share = weight / float(counted)
    rest = counted - weight
    shape = ("%d of %d input token(s) are the tools block (%.0f%%)"
             % (weight, counted, share * 100))
    if rest > 0:
        shape += (", against %d token(s) of system and messages, a ratio of "
                  "%.1f to 1" % (rest, weight / float(rest)))
    if share >= dominate:
        return ("schema-dominates",
                shape + ". The machinery outweighs the conversation on every "
                "call, cached or not.")
    if share >= heavy:
        return ("schema-heavy",
                shape + ". Not dominant, and still the single largest stable "
                "block in the prompt, which makes it the cheapest thing to "
                "cache.")
    return ("schema-modest", shape + ".")


def defer_candidates(rows, hot=(), keep_eager=1):
    """Tools that could carry defer_loading, and never all of them. Pure.

    The API answers a request whose every tool defers with 400, "All tools have
    defer_loading set". A function able to return the whole list is a function
    that has already caused an outage, so at least one tool always stays eager
    whatever the arithmetic says.
    """
    names = [str(r.get("name")) for r in rows or [] if r.get("name")]
    if len(names) <= keep_eager:
        return []
    hot_set = {str(h) for h in hot or []}
    candidates = [n for n in names if n not in hot_set]
    if len(candidates) >= len(names):
        heaviest = sorted(rows, key=lambda r: -_int(r.get("tokens")))
        eager = {str(r.get("name")) for r in heaviest[:max(1, keep_eager)]}
        candidates = [n for n in names if n not in eager]
    return candidates


def monthly_cost(tokens_per_call, calls_per_day, rate_per_mtok, days=30):
    """What one per-call token count costs in a month. Pure. None if unpriced."""
    tokens = _int(tokens_per_call)
    calls = _int(calls_per_day)
    try:
        rate = float(rate_per_mtok)
    except (TypeError, ValueError):
        return None
    if tokens <= 0 or calls <= 0 or rate <= 0:
        return None
    return tokens * calls * int(days) / 1_000_000.0 * rate


def window_share(total, window):
    """Share of the model context window spent before the user speaks. Pure."""
    size = _int(window)
    if size <= 0:
        return None
    return min(1.0, _int(total) / float(size))


def get(session, path):
    r = session.get(API + path, timeout=30)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: ANTHROPIC_API_KEY has to be a "
                         "workspace key" % r.status_code)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def count(session, body):
    """The one non-GET call. It creates nothing, generates nothing, bills nothing."""
    r = session.post(API + "/messages/count_tokens", json=body, timeout=120)
    if r.status_code >= 400:
        log.warning("count_tokens answered %d: %s", r.status_code, r.text[:200])
        return None
    return _int((r.json() or {}).get("input_tokens"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--payload", action="append", default=[], required=True,
                    metavar="FILE", help="a JSON file holding a real request body")
    ap.add_argument("--calls-per-day", type=int, default=10000,
                    help="calls of this shape per day, for the monthly price")
    ap.add_argument("--input-rate", type=float, default=3.0,
                    help="your model's uncached input rate per million tokens")
    ap.add_argument("--hot", action="append", default=[],
                    help="a tool name that must stay eagerly loaded; repeatable")
    ap.add_argument("--no-per-tool", action="store_true",
                    help="skip the per-tool ablation")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a workspace key")
        return 2

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION,
                            "content-type": "application/json"})

    checked = 0
    bad = 0
    for path in args.payload:
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
        checked += 1

        total = count(session, countable(body))
        base = count(session, without_tools(body))
        if total is None or base is None:
            log.warning("could not measure %s", path)
            continue

        state, detail = classify(total, base)
        line = "%-18s %-24s %s" % (state, path, detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
        else:
            log.info(line)

        model = str(body.get("model") or "")
        kind = choice_kind(body)
        fixed = system_prompt_tokens(model, kind)
        if fixed is None:
            log.info("  no published tool-use system prompt size for %r, so "
                     "the fixed charge cannot be separated out here", model)
        else:
            log.info("  %d of the overhead is the automatic tool-use system "
                     "prompt for %s at tool_choice %s", fixed, model, kind)

        rows = []
        if not args.no_per_tool:
            for name in tool_names(body):
                one = count(session, without_tool(body, name))
                if one is None:
                    continue
                rows.append({"name": name, "tokens": max(0, total - one)})
            rows.sort(key=lambda r: -r["tokens"])
            residual, measured = fixed_overhead(overhead(total, base), rows)
            log.info("  the fixed charge no ablation removes: %d token(s); "
                     "your schemas account for %d", residual, measured)
            if rows:
                log.info("  heaviest: %s", ", ".join(
                    "%s %d" % (r["name"], r["tokens"]) for r in rows[:3]))

        window = get(session, "/models/" + model).get("max_input_tokens") if model else None
        share = window_share(total, window)
        if share is not None:
            log.info("  %.0f%% of the %d token context window is spent before "
                     "the user says anything. Whether a real conversation still "
                     "fits is the context-overflow question, not this one.",
                     share * 100, _int(window))

        price = monthly_cost(overhead(total, base), args.calls_per_day,
                             args.input_rate)
        if price is not None:
            log.info("  at %d call(s) a day and %.2f per million input tokens "
                     "that is %.2f a month, uncached", args.calls_per_day,
                     args.input_rate, price)

        if state in FINDINGS:
            log.warning("  repair: put a cache_control breakpoint after the "
                        "tools block. A read costs 0.1x base input, and tools "
                        "are the most stable thing in the prompt.")
            log.warning("  repair: editing any tool description after that "
                        "invalidates the tools, the system prompt and the "
                        "messages behind them. Batch tool edits.")
            candidates = defer_candidates(rows, args.hot)
            if candidates:
                log.warning("  repair: defer_loading on rarely used tools only "
                            "(%s). Never on all of them: the API answers 400, "
                            "All tools have defer_loading set. Which are rare "
                            "is a call-coverage question this script cannot "
                            "answer.", ", ".join(candidates[:5]))

    log.info("%d payload(s) measured, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
