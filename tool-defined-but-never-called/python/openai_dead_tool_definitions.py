"""Find OpenAI tool definitions that are sent on every call and never chosen.

Read only. One GET per stored response id, using a project key. No completion
is created and nothing is written; /v1/responses is read, never posted to.

There is no list endpoint for stored responses, so the sample comes from a file
of ids you supply. Every claim this script makes is bounded by that sample and
the output says so: "never called in 412 turns" is the finding, not "never
called".

The repair is printed, never performed. Pruning a tool registry is a deploy.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_dead_tool_definitions")

API = "https://api.openai.com/v1"

# Output item types that represent the model choosing a tool. Anything else in
# output[] is a message, a reasoning item or a hosted tool call, and none of
# those is evidence that one of your function definitions was selected.
CALL_TYPES = ("function_call", "custom_tool_call")

# The documented guidance is fewer than twenty tools available at the start of
# a turn. Past that, selection quality falls and it falls on the vaguest
# descriptions first.
CROWD_CEILING = 20

FINDINGS = ("never-called", "never-offered")


def _int(value):
    """Read a count as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_ids(text):
    """Response ids out of a plain text file. Pure. Order kept, duplicates dropped.

    Also the guard that stops an arbitrary line of a file becoming a URL path
    segment. Anything that is not a plausible response id is discarded rather
    than sent, because a script that interpolates unvalidated text into a
    provider URL is one typo away from requesting something else entirely.
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
    """The function name out of either tool shape. Pure. None when absent.

    The Responses API puts name at the top level of the tool object; Chat
    Completions nests it under function. A reader that knows only one shape
    reports every tool as undeclared on half the corpus.
    """
    if not isinstance(tool, dict):
        return None
    name = tool.get("name")
    if not name and isinstance(tool.get("function"), dict):
        name = tool["function"].get("name")
    name = str(name or "").strip()
    return name or None


def declared_tools(response):
    """Every named tool the request declared, with its serialized size. Pure.

    Size in characters, never tokens. Hosted tools carry no name and are
    skipped: web search is not a definition you wrote and not one you can prune.
    """
    out = {}
    for tool in (response or {}).get("tools") or []:
        name = tool_name(tool)
        if name is None:
            continue
        try:
            size = len(json.dumps(tool, separators=(",", ":"), sort_keys=True))
        except (TypeError, ValueError):
            size = 0
        out[name] = max(out.get(name, 0), size)
    return out


def called_tools(response):
    """Tool names the model actually chose in one response, counted. Pure."""
    counts = {}
    for item in (response or {}).get("output") or []:
        if not isinstance(item, dict) or item.get("type") not in CALL_TYPES:
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts


def choice_mode(response):
    """How free the model was to pick a tool in this turn. Pure.

    Returns "free", "blocked", or "named:<tool>". An absent tool_choice is
    auto, which is free. This is the difference between a tool the model
    ignored and a tool your own request never put on the table.
    """
    choice = (response or {}).get("tool_choice")
    if choice is None:
        return "free"
    if isinstance(choice, str):
        lowered = choice.strip().lower()
        if lowered == "none":
            return "blocked"
        return "free"
    if isinstance(choice, dict):
        name = tool_name(choice)
        if name:
            return "named:" + name
        return "free"
    return "free"


def fold(responses):
    """Fold a sample of stored responses into one corpus. Pure.

    Declarations and offers are counted separately on purpose. A tool declared
    on four hundred turns and offered on none of them is not dead weight, it is
    a tool_choice that never let the model near it, and the two have nothing in
    common as repairs.
    """
    corpus = {"sampled": 0, "with_tools": 0, "widest_turn": 0, "calls": 0,
              "declared": {}, "offered": {}, "called": {}}
    for response in responses or []:
        if not isinstance(response, dict):
            continue
        corpus["sampled"] += 1
        declared = declared_tools(response)
        calls = called_tools(response)
        for name, count in calls.items():
            corpus["called"][name] = corpus["called"].get(name, 0) + count
            corpus["calls"] += count
        if not declared:
            continue
        corpus["with_tools"] += 1
        corpus["widest_turn"] = max(corpus["widest_turn"], len(declared))
        mode = choice_mode(response)
        for name, size in declared.items():
            row = corpus["declared"].setdefault(name, {"turns": 0, "chars": 0})
            row["turns"] += 1
            row["chars"] = max(row["chars"], size)
            if mode == "blocked":
                continue
            if mode.startswith("named:") and mode[len("named:"):] != name:
                continue
            corpus["offered"][name] = corpus["offered"].get(name, 0) + 1
    return corpus


def coverage(corpus):
    """One row per declared tool. Pure. Least used and most expensive first."""
    rows = []
    for name, row in ((corpus or {}).get("declared") or {}).items():
        rows.append({
            "name": name,
            "turns": _int(row.get("turns")),
            "chars": _int(row.get("chars")),
            "offered": _int(((corpus or {}).get("offered") or {}).get(name)),
            "calls": _int(((corpus or {}).get("called") or {}).get(name)),
        })
    rows.sort(key=lambda r: (r["calls"], -r["chars"], r["name"]))
    return rows


def orphan_calls(corpus):
    """Names the model called that no sampled request declared. Pure.

    Not a fault in the registry: it means the sample mixes two configurations,
    and a set difference computed across two configurations is meaningless.
    """
    declared = set(((corpus or {}).get("declared") or {}))
    return sorted(n for n in ((corpus or {}).get("called") or {}) if n not in declared)


def classify(row, min_offered=50, rare=0.01):
    """Classify one tool's coverage across the sample. Pure. Returns (state, detail)."""
    row = row or {}
    name = str(row.get("name") or "unknown")
    turns = _int(row.get("turns"))
    offered = _int(row.get("offered"))
    calls = _int(row.get("calls"))

    if turns and offered == 0:
        return ("never-offered",
                "declared in %d turn(s), free to be chosen in 0 of them. "
                "tool_choice ruled it out every time, so the model never "
                "declined it and rewriting the description changes nothing."
                % turns)
    if offered < min_offered:
        return ("too-small-a-sample",
                "offered in %d turn(s), under the floor of %d. Not enough to "
                "call anything dead." % (offered, min_offered))
    if calls == 0:
        return ("never-called",
                "offered in %d of %d turn(s), called 0 time(s), %d schema "
                "char(s). Sent and billed on every one of those turns."
                % (offered, turns, _int(row.get("chars"))))
    share = calls / float(offered)
    if share < rare:
        return ("rarely-called",
                "offered in %d turn(s), called %d time(s) (%.1f%%). Worth "
                "keeping and worth not sending on every turn."
                % (offered, calls, share * 100))
    return ("called",
            "offered in %d turn(s), called %d time(s) (%.1f%%)."
            % (offered, calls, share * 100))


def dead_weight(rows, min_offered=50, rare=0.01):
    """Share of the declared schema, in characters, that nothing ever calls. Pure.

    Characters, and the docstring is the place to be blunt about it: this is
    not a token count and must never be read as one. Tokens are measured
    exactly and for free by the token-overhead note, and a character count
    dressed up as a token count is worse than no number at all.
    """
    total = 0
    dead = 0
    for row in rows or []:
        chars = _int(row.get("chars"))
        total += chars
        if classify(row, min_offered, rare)[0] == "never-called":
            dead += chars
    if total <= 0:
        return None
    return dead / float(total)


def crowding(widest_turn, ceiling=CROWD_CEILING):
    """What the widest turn in the sample looked like. Pure.

    Above the guidance the finding changes shape: the problem is no longer any
    one description, it is that the model is choosing among too many at once,
    and the repair is a narrower turn rather than better prose.
    """
    widest = _int(widest_turn)
    if widest <= 0:
        return ("no-tools", "no sampled response declared any named tool")
    if widest > ceiling:
        return ("crowded",
                "the widest turn offered %d tools, above the guidance of fewer "
                "than %d. Selection quality falls with crowding and it falls on "
                "the vaguest descriptions first." % (widest, ceiling))
    return ("within-guidance",
            "the widest turn offered %d tool(s), inside the guidance of fewer "
            "than %d" % (widest, ceiling))


def repair_lines(state, name):
    """The repair for one classified tool. Pure."""
    if state == "never-called":
        return [
            "the description probably reads like a signature. Rewrite it as a "
            "selection rule: when to call %s, and when not to." % name,
            "if a call is mandatory, say so with tool_choice required or a "
            "named tool rather than hoping the model picks it up.",
            "if nothing needs it, delete it. It is billed on every turn.",
        ]
    if state == "never-offered":
        return [
            "tool_choice never let the model near %s. Fix the request before "
            "you touch the description." % name,
        ]
    if state == "rarely-called":
        return [
            "keep %s, but stop sending it on every turn. allowed_tools narrows "
            "the set for the turns where it is plausible." % name,
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
    ap.add_argument("--min-offered", type=int, default=50,
                    help="turns a tool must have been offered in before "
                         "silence counts as evidence (default 50)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print tools that are being called normally")
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

    responses = []
    missing = 0
    for response_id in ids:
        body = get(session, "/responses/" + response_id)
        if body is None:
            missing += 1
            continue
        responses.append(body)
    if missing:
        log.info("%d of %d id(s) no longer resolve; stored responses are not "
                 "kept forever", missing, len(ids))

    corpus = fold(responses)
    rows = coverage(corpus)
    if not rows:
        log.info("no named tools declared in %d sampled response(s)",
                 corpus["sampled"])
        return 0

    orphans = orphan_calls(corpus)
    if orphans:
        log.warning("called but never declared in this sample: %s. The sample "
                    "mixes two configurations, so the set difference below is "
                    "not reliable.", ", ".join(orphans))

    bad = 0
    for row in rows:
        state, detail = classify(row, args.min_offered)
        line = "%-19s %-22s %s" % (state, row["name"], detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
            for repair in repair_lines(state, row["name"]):
                log.warning("  repair: %s", repair)
        elif state == "rarely-called":
            log.warning(line)
            for repair in repair_lines(state, row["name"]):
                log.warning("  repair: %s", repair)
        elif args.show_all or state == "too-small-a-sample":
            log.info(line)

    log.info("%d declared tool(s) over %d response(s), %d finding(s)",
             len(rows), corpus["sampled"], bad)

    share = dead_weight(rows, args.min_offered)
    if share is not None:
        log.info("%.0f%% of the declared schema, in characters, belongs to "
                 "tools nothing ever called. Characters are not tokens: count "
                 "the block for free against count_tokens before pricing it.",
                 share * 100)

    state, detail = crowding(corpus["widest_turn"])
    if state == "crowded":
        log.warning("%-19s %s", state, detail)
        log.warning("  repair: narrow the turn with allowed_tools rather than "
                    "rewriting one description at a time.")
    else:
        log.info("%-19s %s", state, detail)

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
