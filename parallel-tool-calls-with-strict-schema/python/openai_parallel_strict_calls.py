"""Find OpenAI turns where parallel tool calls voided a strict schema.

Read only. One GET per stored response id, using a project key. No completion
is created and nothing is written; /v1/responses is read, never posted to.

Structured Outputs is not supported alongside parallel function calls, and
parallel_tool_calls defaults to true. So a turn that returns more than one
function_call item while any tool declares strict: true came back without the
guarantee the parser is relying on, and it did so with an HTTP 200.

The repair is printed, never performed. One boolean is still a deploy.
"""
import argparse
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_parallel_strict_calls")

API = "https://api.openai.com/v1"

CALL_TYPES = ("function_call", "custom_tool_call")

FINDINGS = ("strict-void",)


def _int(value):
    """Read a count as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_ids(text):
    """Response ids out of a plain text file. Pure. Order kept, duplicates dropped.

    Also the guard that stops an arbitrary line of a file becoming a URL path
    segment: anything that is not a plausible response id is discarded rather
    than interpolated into a provider URL.
    """
    out = []
    seen = set()
    for line in str(text or "").splitlines():
        candidate = line.split("#", 1)[0].strip()
        if not candidate or not candidate.startswith("resp_"):
            continue
        if not all(ch.isalnum() or ch in "_-" for ch in candidate):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def tool_name(tool):
    """The function name out of either tool shape. Pure. None when absent."""
    if not isinstance(tool, dict):
        return None
    name = tool.get("name")
    if not name and isinstance(tool.get("function"), dict):
        name = tool["function"].get("name")
    name = str(name or "").strip()
    return name or None


def declared_names(response):
    """Every named tool the request declared. Pure. Sorted."""
    out = set()
    for tool in (response or {}).get("tools") or []:
        name = tool_name(tool)
        if name:
            out.add(name)
    return sorted(out)


def strict_tools(response):
    """Tools declaring strict: true, in either shape. Pure. Sorted.

    strict false and strict absent are the same thing here and neither counts.
    A note about a voided guarantee has to be certain the guarantee was claimed.
    """
    out = set()
    for tool in (response or {}).get("tools") or []:
        if not isinstance(tool, dict):
            continue
        strict = tool.get("strict")
        if strict is not True and isinstance(tool.get("function"), dict):
            strict = tool["function"].get("strict")
        if strict is not True:
            continue
        name = tool_name(tool)
        if name:
            out.add(name)
    return sorted(out)


def parallel_allowed(response):
    """Could the model return more than one tool call in this turn? Pure.

    An absent parallel_tool_calls is true, and reading it as false is the exact
    mistake that makes this whole class of failure invisible.
    """
    value = (response or {}).get("parallel_tool_calls")
    return value is not False


def function_calls(response):
    """The tool calls in one turn, in order. Pure.

    call_id is kept because half the repair depends on it: a handler keyed on
    call_id cannot double-apply when the same tool is called twice.
    """
    out = []
    for item in (response or {}).get("output") or []:
        if not isinstance(item, dict) or item.get("type") not in CALL_TYPES:
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name, "call_id": str(item.get("call_id") or "")})
    return out


def duplicate_names(calls):
    """Tool names called more than once in one turn. Pure.

    A separate fault with the same trigger. It costs side effects rather than
    correctness, and turning parallel calls off is not the only fix for it.
    """
    counts = {}
    for call in calls or []:
        name = str((call or {}).get("name") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return {name: n for name, n in counts.items() if n > 1}


def classify(response):
    """Classify one turn. Pure. Returns (state, detail).

    The unit is the turn and not the corpus, because the guarantee is voided or
    kept per response and a rate computed over anything else means nothing.
    """
    declared = declared_names(response)
    if not declared:
        return ("no-tools", "no named tools declared in this turn")

    strict = strict_tools(response)
    calls = function_calls(response)
    parallel = parallel_allowed(response)
    names = ", ".join(c["name"] for c in calls) or "none"

    if not strict:
        if len(calls) > 1:
            return ("fanout-no-strict",
                    "%d function_call item(s) in one turn (%s) and no tool "
                    "declares strict. There was no guarantee to void here: the "
                    "arguments were never validated by the API at all, which "
                    "is a different fault with a different repair."
                    % (len(calls), names))
        return ("no-strict-declared",
                "%d tool(s) declared, none of them strict. Nothing in this turn "
                "was schema-guaranteed." % len(declared))

    if not parallel:
        return ("strict-serialised",
                "strict declared on %d tool(s) and parallel_tool_calls is "
                "false. The guarantee holds." % len(strict))

    if len(calls) > 1:
        return ("strict-void",
                "%d function_call item(s) in one turn with strict declared and "
                "parallel_tool_calls left on (%s). Structured Outputs is not "
                "supported alongside parallel calls, so these argument objects "
                "carry no schema guarantee." % (len(calls), names))

    return ("strict-at-risk",
            "strict declared on %d tool(s) with parallel_tool_calls left on, "
            "and this turn happened to return %d call(s). The configuration is "
            "loaded; it did not fire here." % (len(strict), len(calls)))


def exposure(states):
    """How often the fan-out that voids the guarantee actually happens. Pure.

    The denominator is turns that were at risk, never all turns: a rate over
    responses that declared no strict tools flatters the number by however much
    unrelated traffic happened to be in the sample. None when nothing was at
    risk, because a rate over an empty denominator invents a number.
    """
    at_risk = sum(1 for s in states or [] if s in ("strict-void", "strict-at-risk"))
    void = sum(1 for s in states or [] if s == "strict-void")
    if at_risk <= 0:
        return {"at_risk": 0, "void": void, "rate": None}
    return {"at_risk": at_risk, "void": void, "rate": void / float(at_risk)}


def unvalidated_calls(rows):
    """Argument objects that came back with no guarantee behind them. Pure.

    Counted only in the turns where the guarantee was actually void. The number
    the parser cares about is objects, not turns.
    """
    return sum(_int(row.get("calls")) for row in rows or []
               if row.get("state") == "strict-void")


def repair_lines(state):
    """The repair for one classified turn. Pure."""
    if state == "strict-void":
        return [
            "set parallel_tool_calls false whenever strict schemas matter. It "
            "defaults to true, which is why this was never a decision anyone "
            "made.",
            "if you need the fan-out for latency, drop strict and validate the "
            "arguments yourself. Do not keep a guarantee you know is not held.",
            "key every tool handler on call_id and make it idempotent, so a "
            "duplicate parallel call cannot double-apply.",
        ]
    if state == "strict-at-risk":
        return [
            "this turn was fine and the configuration is not. The same request "
            "shape returns several calls whenever the model decides to, so set "
            "parallel_tool_calls false before it does.",
        ]
    if state == "fanout-no-strict":
        return [
            "no schema guarantee was in place to lose. Validate tool arguments "
            "in your own handler, or declare strict and serialise the calls.",
        ]
    return []


def get(session, path):
    r = session.get(API + path, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: OPENAI_API_KEY needs read access to "
                         "stored responses in this project" % r.status_code)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--responses", metavar="FILE",
                    help="a text file of stored response ids, one per line")
    ap.add_argument("--response-id", action="append", default=[],
                    help="a single response id; repeatable")
    ap.add_argument("--show-all", action="store_true",
                    help="also print turns that are correctly configured")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key that can read stored "
                  "responses")
        return 2

    ids = list(args.response_id)
    if args.responses:
        try:
            with open(args.responses, "r", encoding="utf-8") as fh:
                ids.extend(parse_ids(fh.read()))
        except OSError as exc:
            log.error("could not read %s: %s", args.responses, exc)
            return 2
    ids = parse_ids("\n".join(ids))
    if not ids:
        log.error("no usable response ids. /v1/responses cannot be listed, so "
                  "the sample has to come from your own request log")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    rows = []
    bad = 0
    read = 0
    for response_id in ids:
        body = get(session, "/responses/" + response_id)
        if body is None:
            continue
        read += 1
        state, detail = classify(body)
        calls = function_calls(body)
        rows.append({"id": response_id, "state": state, "calls": len(calls)})

        line = "%-19s %-14s %s" % (state, response_id, detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
            log.warning("  calls: %s", ", ".join(c["name"] for c in calls))
        elif state == "fanout-no-strict":
            log.warning(line)
        elif args.show_all or state == "strict-at-risk":
            log.info(line)

        dupes = duplicate_names(calls)
        if dupes:
            log.warning("  duplicate: %s. Handlers keyed on the tool name "
                        "rather than call_id will double apply.",
                        "; ".join("%s called %d time(s) in one turn" % (n, c)
                                  for n, c in sorted(dupes.items())))

        if state in ("strict-void", "fanout-no-strict"):
            for repair in repair_lines(state):
                log.warning("  repair: %s", repair)

    shape = exposure([r["state"] for r in rows])
    if shape["rate"] is None:
        log.info("no turn in this sample declared a strict tool with parallel "
                 "calls left on, so there is no exposure to report")
    else:
        log.info("exposure: %d of %d at-risk turn(s) fanned out (%.1f%%), "
                 "covering %d argument object(s) with no guarantee",
                 shape["void"], shape["at_risk"], shape["rate"] * 100,
                 unvalidated_calls(rows))
        if shape["void"] == 0:
            log.warning("  every at-risk turn happened to return one call. That "
                        "is luck, not configuration: set parallel_tool_calls "
                        "false before it stops being lucky.")

    log.info("%d response(s) read, %d finding(s)", read, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
