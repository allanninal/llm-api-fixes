"""Find stored OpenAI responses whose JSON schema was never actually enforced.

Read only. GET /v1/responses/{response_id} for each id you supply, with a
project key set to Read Only. There is no list endpoint for /v1/responses, so
the ids come from your own records: one id per line in a file.

Structured Outputs guarantees schema adherence only when strict is true. With
strict absent or false the schema degrades to a hint the model usually follows,
and the request is accepted either way with no warning of any kind. The stored
response echoes the format it was given, which is the only place outside your
source tree where the flag can be read back.

When strict is off, the interesting question is why, and the answer is almost
always that the schema cannot satisfy the strict subset. So this script does
not stop at the flag: it walks the schema and prints every rule that would have
to be fixed before strict: true could be turned on.

The repair is printed, never performed.
"""
import argparse
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_advisory_schema")

API = "https://api.openai.com/v1"

FINDINGS = ("advisory-schema", "no-schema", "advisory-tools")

# Constrained decoding ignores these entirely. A schema that carries them is
# not wrong, but the constraints they express are not enforced by anyone.
UNENFORCED_KEYWORDS = ("minLength", "maxLength", "pattern", "format", "minimum",
                       "maximum", "multipleOf", "minItems", "maxItems",
                       "uniqueItems", "default")

# The documented ceilings for a strict schema.
MAX_DEPTH = 5
MAX_PROPERTIES = 5000
MAX_ENUM_VALUES = 1000


def declared_format(response):
    """The output format the response was generated under. Pure.

    Returns (kind, name, strict, schema). kind is json_schema, json_object,
    text or none. Read from the response rather than from your source tree,
    because the constant in the repository is not necessarily what the running
    deploy sent.
    """
    response = response or {}
    fmt = ((response.get("text") or {}).get("format")
           or response.get("response_format") or {})
    if not isinstance(fmt, dict) or not fmt:
        return ("none", None, None, None)

    kind = str(fmt.get("type") or "none")
    if kind == "json_schema":
        # The Responses API flattens the schema onto the format object; Chat
        # Completions nests it under json_schema. Both shapes are stored.
        inner = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), dict) else fmt
        return ("json_schema", inner.get("name"), inner.get("strict"),
                inner.get("schema"))
    return (kind, None, None, None)


def strict_state(kind, strict):
    """What the declared format actually promises. Pure."""
    if kind == "json_schema":
        return "enforced" if strict is True else "advisory"
    if kind == "json_object":
        return "no-schema"
    if kind in ("text", "none"):
        return "free-text"
    return "unknown-format"


def schema_size(schema, depth=1):
    """Count properties, depth and the largest enum in a schema. Pure.

    Returned as a dict rather than printed, because the interesting comparison
    is against the documented ceilings and those change more often than this
    walk does.
    """
    totals = {"properties": 0, "depth": depth, "enum": 0}
    if not isinstance(schema, dict):
        return totals
    if isinstance(schema.get("enum"), list):
        totals["enum"] = max(totals["enum"], len(schema["enum"]))
    children = []
    properties = schema.get("properties")
    if isinstance(properties, dict):
        totals["properties"] += len(properties)
        children.extend(properties.values())
    items = schema.get("items")
    if isinstance(items, dict):
        children.append(items)
    for group in ("anyOf", "oneOf", "allOf"):
        if isinstance(schema.get(group), list):
            children.extend(x for x in schema[group] if isinstance(x, dict))
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        children.extend(x for x in defs.values() if isinstance(x, dict))

    for child in children:
        below = schema_size(child, depth + 1)
        totals["properties"] += below["properties"]
        totals["depth"] = max(totals["depth"], below["depth"])
        totals["enum"] = max(totals["enum"], below["enum"])
    return totals


def schema_blockers(schema, path="$", depth=1):
    """Every reason strict: true would be refused for this schema. Pure.

    This is the part of the note that pays for itself. Telling somebody to set
    strict: true is useless on its own, because they tried that, the request
    400ed, and the flag came back out. The list of rules the schema breaks is
    the actual work.
    """
    problems = []
    if not isinstance(schema, dict):
        return ["%s: not a schema object" % path] if depth == 1 else problems

    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else ([kinds] if kinds else [])
    kinds = [str(k) for k in kinds]

    if depth == 1:
        if any(schema.get(group) for group in ("anyOf", "oneOf", "allOf")):
            problems.append("$: the root may not be anyOf, oneOf or allOf; it "
                            "must be a plain object")
        elif "object" not in kinds:
            problems.append("$: the root type must be object, not %s"
                            % (", ".join(kinds) or "unset"))

    if "object" in kinds:
        if schema.get("additionalProperties") is not False:
            problems.append("%s: needs additionalProperties: false" % path)
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = set(required) if isinstance(required, list) else set()
        missing = sorted(set(properties) - required)
        if missing:
            problems.append("%s: every property must be listed in required; "
                            "missing %s. Use a nullable type for the optional "
                            "ones rather than leaving them out."
                            % (path, ", ".join(missing)))

    present = [k for k in UNENFORCED_KEYWORDS if k in schema]
    if present:
        problems.append("%s: %s are silently unenforced under constrained "
                        "decoding. Keep them for your own validator if you "
                        "like, but do not rely on the model honouring them."
                        % (path, ", ".join(present)))

    if depth > MAX_DEPTH:
        problems.append("%s: nested %d levels deep, past the limit of %d"
                        % (path, depth, MAX_DEPTH))
        return problems

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name in sorted(properties):
            problems.extend(schema_blockers(properties[name],
                                            "%s.%s" % (path, name), depth + 1))
    items = schema.get("items")
    if isinstance(items, dict):
        problems.extend(schema_blockers(items, path + "[]", depth + 1))
    return problems


def loose_tools(response):
    """Tools echoed on the response whose strict flag is not true. Pure.

    Per tool, because strict is declared per tool. A response can carry a
    strict text format and a function definition with no strict flag at all,
    and the guarantee covers only the half that asked for it.
    """
    loose = []
    for tool in (response or {}).get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if str(tool.get("type") or "function") != "function":
            continue
        inner = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if inner.get("strict") is not True:
            loose.append(str(inner.get("name") or "unnamed"))
    return loose


def classify(response):
    """Classify one stored response. Pure. Returns (state, detail).

    Nothing here reads the output text. Whether this particular call happened
    to produce a well-shaped object is not the point: the point is that no call
    made under this format was ever obliged to.
    """
    kind, name, strict, schema = declared_format(response)
    state = strict_state(kind, strict)
    loose = loose_tools(response)
    label = ("schema %r" % name) if name else "the declared schema"

    if state == "advisory":
        return ("advisory-schema",
                "%s was attached with strict %s, so it is a hint the model "
                "usually follows rather than a guarantee. Valid JSON of the "
                "wrong shape is a legal outcome here."
                % (label, "false" if strict is False else "absent"))
    if state == "no-schema":
        return ("no-schema",
                "Legacy json_object mode: the output is guaranteed to be valid "
                "JSON and nothing else. No schema was ever attached, so no "
                "shape was ever promised.")
    if state == "free-text":
        return ("free-text",
                "No output format was declared, so there is no contract to "
                "enforce and nothing to report.")
    if state == "unknown-format":
        return ("unknown-format",
                "Format type %r is not one this script knows. Read the raw "
                "record before drawing a conclusion." % kind)

    if loose:
        return ("advisory-tools",
                "The text format is strict, but %d tool definition(s) are not: "
                "%s. Tool arguments are constrained per tool, and an unstrict "
                "tool is unconstrained." % (len(loose), ", ".join(loose)))
    return ("enforced",
            "%s was attached with strict: true, and no tool beside it is "
            "loose." % label)


def repair_lines(response, state):
    """The repair, built from the schema this response actually carried. Pure."""
    _kind, _name, _strict, schema = declared_format(response)
    if state == "free-text" or state == "unknown-format":
        return []
    if state == "enforced":
        return []

    lines = []
    if state == "no-schema":
        lines.append("Move from json_object to a json_schema format with "
                     "strict: true. JSON mode promises syntax and nothing "
                     "about shape, which is why your validator is the first "
                     "thing that ever sees the mismatch.")
    if state == "advisory-tools":
        lines.append("Set strict: true on every tool as well as on the text "
                     "format, with additionalProperties: false and every "
                     "parameter listed in required.")

    blockers = schema_blockers(schema) if schema else []
    if blockers:
        lines.append("strict: true would be refused for this schema until "
                     "these are fixed:")
        lines.extend("  " + b for b in blockers)
    elif state == "advisory-schema":
        lines.append("This schema already satisfies the strict subset, so "
                     "setting strict: true is a one-line change. Somebody "
                     "dropped the flag and the request kept succeeding.")

    if schema:
        size = schema_size(schema)
        if size["properties"] > MAX_PROPERTIES:
            lines.append("The schema declares %d properties, past the limit of "
                         "%d." % (size["properties"], MAX_PROPERTIES))
        if size["enum"] > MAX_ENUM_VALUES:
            lines.append("The largest enum holds %d values, past the limit of "
                         "%d." % (size["enum"], MAX_ENUM_VALUES))
    return lines


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
    ap.add_argument("--show-all", action="store_true",
                    help="also print responses whose schema is enforced")
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
        checked += 1
        if stored is None:
            log.warning("%-18s %s  not found. Stored responses expire, and a "
                        "response created without storage was never readable.",
                        "unreadable", response_id)
            continue
        state, detail = classify(stored)
        line = "%-18s %s  %s" % (state, response_id, detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
            for repair in repair_lines(stored, state):
                log.warning("  repair: %s", repair)
        elif args.show_all or state not in ("enforced", "free-text"):
            log.info(line)

    log.info("%d response(s) checked, %d with a schema nobody was holding to",
             checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
