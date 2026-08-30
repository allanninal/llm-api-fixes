"""Check every stored tool call against the tool schema declared beside it.

Read only. GET /v1/responses/{response_id} for each id you supply, with a
project key set to Read Only. There is no list endpoint for /v1/responses, so
the ids come from your own records: one id per line in a file.

Function arguments come back JSON encoded, as a string, and the documentation
is explicit that the string may be malformed. Two quite different faults arrive
through that one field:

  * the string will not parse, which a careful try/except catches;
  * the string parses perfectly and describes a call your handler cannot
    accept, which nothing around json.loads will ever catch.

The second one is why this script exists. The response object carries the tool
definitions it was generated with, so the declared schema and the emitted call
can be compared without reading a line of your source, and the thing that
throws in production is your dispatcher rather than the API.

The repair is printed, never performed.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_tool_call_arguments")

API = "https://api.openai.com/v1"

FINDINGS = ("arguments-violate-schema", "arguments-unparseable", "unknown-tool")

# JSON Schema type names mapped onto what a parsed document can actually be.
TYPE_TESTS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
}


def function_calls(response):
    """Every function call in a stored response. Pure.

    Returns dicts with name, call_id and the raw arguments string, in the order
    the model emitted them. Both surfaces: the Responses API puts a
    function_call item in output[], Chat Completions puts tool_calls on the
    message.
    """
    calls = []
    response = response or {}
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        calls.append({"name": str(item.get("name") or ""),
                      "call_id": str(item.get("call_id") or item.get("id") or ""),
                      "arguments": item.get("arguments")})
    for choice in response.get("choices") or []:
        for call in (choice.get("message") or {}).get("tool_calls") or []:
            fn = call.get("function") or {}
            calls.append({"name": str(fn.get("name") or ""),
                          "call_id": str(call.get("id") or ""),
                          "arguments": fn.get("arguments")})
    return calls


def declared_tools(response):
    """The tool definitions the response was generated with. Pure.

    Keyed by name, carrying the parameter schema and the strict flag. Taken
    from the response rather than from your source tree, because the definition
    that matters is the one that was actually sent.
    """
    tools = {}
    for tool in (response or {}).get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if str(tool.get("type") or "function") != "function":
            continue
        inner = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(inner.get("name") or "")
        if name:
            tools[name] = {"parameters": inner.get("parameters"),
                           "strict": inner.get("strict") is True}
    return tools


def parse_arguments(text):
    """Parse one arguments string. Pure. Returns (value, error).

    error is None on success. An empty string is a legal way for a model to
    call a tool that takes nothing, so it parses to an empty object rather than
    failing, which is a distinction a naive json.loads gets wrong on its first
    day in production.
    """
    if text is None:
        return (None, "the arguments field is absent")
    if isinstance(text, dict):
        # Some SDKs hand back a parsed object. Nothing to do.
        return (text, None)
    body = str(text).strip()
    if not body:
        return ({}, None)
    try:
        value = json.loads(body)
    except ValueError as exc:
        return (None, str(exc))
    if not isinstance(value, dict):
        return (None, "arguments parsed to %s, not an object"
                % type(value).__name__)
    return (value, None)


def schema_violations(value, schema, path="arguments"):
    """Where a parsed argument object departs from its declared schema. Pure.

    Deliberately small: types, required keys, unexpected keys and enums. Those
    four cover the failures that actually reach a dispatcher, and a full JSON
    Schema implementation in a field note would be a library nobody asked for.
    """
    problems = []
    if not isinstance(schema, dict) or not schema:
        return problems

    raw = schema.get("type")
    kinds = raw if isinstance(raw, list) else ([raw] if raw else [])
    kinds = [str(k) for k in kinds]
    known = [k for k in kinds if k in TYPE_TESTS]
    if known and not any(TYPE_TESTS[k](value) for k in known):
        got = "null" if value is None else type(value).__name__
        return ["%s: expected %s, got %s" % (path, " or ".join(known), got)]

    choices = schema.get("enum")
    if isinstance(choices, list) and choices and value not in choices:
        problems.append("%s: %r is not one of the %d declared value(s)"
                        % (path, value, len(choices)))

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        for name in required:
            if name not in value:
                problems.append("%s.%s: required and missing" % (path, name))
        if schema.get("additionalProperties") is False:
            for name in sorted(set(value) - set(properties)):
                problems.append("%s.%s: not declared, and the schema forbids "
                                "extra keys" % (path, name))
        for name in sorted(set(value) & set(properties)):
            problems.extend(schema_violations(value[name], properties[name],
                                              "%s.%s" % (path, name)))

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, entry in enumerate(value):
                problems.extend(schema_violations(entry, items,
                                                  "%s[%d]" % (path, index)))
    return problems


def classify(call, tools, truncated=False):
    """Classify one function call. Pure. Returns (state, detail).

    The order matters. A call whose arguments were cut off belongs to the
    truncation note, and saying so before reporting a parse error keeps the
    reader from tuning a tool schema to fix an output ceiling.
    """
    call = call or {}
    name = str(call.get("name") or "")
    tools = tools or {}
    value, error = parse_arguments(call.get("arguments"))

    if error is not None:
        if truncated:
            return ("arguments-truncated",
                    "the arguments string does not parse (%s) and the response "
                    "stopped on the output ceiling, so it was cut mid-write "
                    "rather than written wrongly" % error)
        return ("arguments-unparseable",
                "the arguments string does not parse (%s) and the response "
                "completed, so nothing was constraining the grammar" % error)

    if name not in tools:
        return ("unknown-tool",
                "the arguments parse cleanly and no tool named %r was declared "
                "on this response. A dispatcher that indexes a handler map by "
                "name raises here, not at the parse." % name)

    schema = tools[name].get("parameters")
    problems = schema_violations(value, schema)
    if problems:
        return ("arguments-violate-schema",
                "the arguments parse cleanly and break the declared schema in "
                "%d place(s): %s" % (len(problems), "; ".join(problems)))

    if not tools[name].get("strict"):
        return ("dispatchable-unconstrained",
                "this call matches the schema, but the tool was declared "
                "without strict: true, so nothing guaranteed that it would")
    return ("dispatchable", "parses and matches the declared schema")


def repair_lines(state, name=None):
    """The repair for one state. Pure."""
    if state == "arguments-violate-schema":
        return ["Validate arguments against the tool schema before dispatch, "
                "and feed the validation error back to the model as the tool "
                "result so it can correct itself. A crashed turn teaches the "
                "model nothing; a returned error usually fixes the next call.",
                "Set strict: true on tool %s, with additionalProperties: false "
                "and every parameter listed in required. Without it the schema "
                "is a suggestion." % (name or "this tool")]
    if state == "arguments-unparseable":
        return ["Wrap every argument parse in try/except and return the parse "
                "error to the model as the tool result rather than raising "
                "through the turn.",
                "Set strict: true on the tool so constrained decoding holds the "
                "grammar in the first place."]
    if state == "arguments-truncated":
        return ["Not a schema problem. The output ceiling cut the argument "
                "string mid-write, so raise it and check the response status "
                "before touching any tool call."]
    if state == "unknown-tool":
        return ["Handle an unknown tool name explicitly: return a tool result "
                "saying the tool does not exist. A KeyError out of the handler "
                "map ends the turn and loses the conversation state.",
                "Check that the tool list sent on this call matches the handler "
                "map. A tool renamed on one side only produces exactly this."]
    if state == "dispatchable-unconstrained":
        return ["This call was fine. Set strict: true on the tool anyway, "
                "because nothing about this response promised it would be."]
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


def was_truncated(response):
    """Did this response stop on the output ceiling? Pure."""
    response = response or {}
    if str(response.get("status") or "") == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason")
        return str(reason or "") == "max_output_tokens"
    for choice in response.get("choices") or []:
        if str(choice.get("finish_reason") or "") == "length":
            return True
    return False


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
    ap.add_argument("--show-all", action="store_true",
                    help="also print calls that parse and validate")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY, a project key set to Read Only")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    checked = 0
    bad = 0
    for response_id in read_ids(args.ids):
        stored = fetch_response(session, response_id)
        if stored is None:
            log.warning("%-27s %s  not found. Stored responses expire, and a "
                        "response created without storage was never readable.",
                        "unreadable", response_id)
            continue
        tools = declared_tools(stored)
        truncated = was_truncated(stored)
        calls = function_calls(stored)
        if not calls:
            continue
        if len(calls) > 1:
            log.info("%-27s %s  %d call(s) in one turn", "parallel-calls",
                     response_id, len(calls))
        for call in calls:
            checked += 1
            state, detail = classify(call, tools, truncated)
            line = "%-27s %s %s/%s  %s" % (state, response_id, call["name"],
                                           call["call_id"] or "-", detail)
            if state in FINDINGS:
                bad += 1
                log.warning(line)
                for repair in repair_lines(state, call["name"]):
                    log.warning("  repair: %s", repair)
            elif state == "dispatchable":
                if args.show_all:
                    log.info(line)
            else:
                log.info(line)
                for repair in repair_lines(state, call["name"]):
                    log.info("  note: %s", repair)

    log.info("%d tool call(s) checked, %d your dispatcher cannot use",
             checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
