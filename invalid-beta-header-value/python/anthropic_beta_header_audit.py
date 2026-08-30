"""Grade every anthropic-beta string your code sends, without sending one.

Read only. Every request is a GET: /v1/models to validate a beta name, and
/v1/models plus /v1/files twice each to compare a response with and without a
header. No request body is constructed and nothing is generated or billed.

Two passes, because the two failures do not share a signal. A misspelled or
unentitled name returns 400 and is loud. A name that graduated to GA returns
200 and is silent, and the only read-only evidence of it is that the same GET
returns a different shape with the header than without it.

What a 200 proves is narrow and the script says so: the name is recognised by
the request layer. It is not evidence that the beta still does anything on
/v1/messages, and nothing here claims that it is.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_beta_header_audit")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# The beta names the Models API reference publishes as accepted values. This is
# a dictionary for near-matching a rejected string, never a verdict: it is a
# document, documents lag, and the probe is the authority. A name the API
# accepts that is missing from here is reported as the list being behind.
KNOWN_BETAS = (
    "message-batches-2024-09-24", "prompt-caching-2024-07-31",
    "computer-use-2024-10-22", "computer-use-2025-01-24",
    "computer-use-2025-11-24", "pdfs-2024-09-25",
    "token-counting-2024-11-01", "token-efficient-tools-2025-02-19",
    "output-128k-2025-02-19", "output-300k-2026-03-24",
    "files-api-2025-04-14", "mcp-client-2025-04-04", "mcp-client-2025-11-20",
    "mcp-tunnels-2026-06-22", "dev-full-thinking-2025-05-14",
    "interleaved-thinking-2025-05-14", "code-execution-2025-05-22",
    "extended-cache-ttl-2025-04-11", "context-1m-2025-08-07",
    "context-management-2025-06-27",
    "model-context-window-exceeded-2025-08-26", "skills-2025-10-02",
    "fast-mode-2026-02-01", "user-profiles-2026-03-24",
    "user-profiles-2026-08-18", "advisor-tool-2026-03-01",
    "managed-agents-2026-04-01", "agent-memory-2026-07-22",
    "cache-diagnosis-2026-04-07", "dreaming-2026-04-21",
    "thinking-token-count-2026-05-13", "thinking-display-updates-2026-08-18",
    "server-side-fallback-2026-06-01", "server-side-fallback-2026-07-01",
    "fallback-credit-2026-06-01", "fallback-credit-2026-07-01",
    "mid-conversation-tool-changes-2026-07-01", "compact-2026-01-12",
    "structured-outputs-2025-11-13", "task-budgets-2026-03-13",
    "ce-user-management-2026-07-13",
)

# Endpoint-scoped betas that are not freely combinable. On memory store
# endpoints the first replaces the second and sending both returns 400.
CONFLICTS = (("agent-memory-2026-07-22", "managed-agents-2026-04-01"),)

# The two listings this script can read with a workspace key. Both are GETs and
# both are free. They are the entire evidence base for the shape comparison,
# which is why "no difference here" is reported as a limit and not as health.
DIFF_PATHS = ("/models", "/files")

FINDINGS = ("rejected-typo", "rejected-unknown", "pinned-to-beta-shape",
            "conflicting-pair", "malformed-header")


def split_betas(raw):
    """(names, faults) from one anthropic-beta header value. Pure.

    Multiple betas travel in one comma-separated header, so the string itself
    can be wrong in ways that have nothing to do with spelling: a trailing
    comma leaves an empty segment, a duplicate is silently pointless, and an
    embedded space is not part of any documented name.
    """
    names = []
    faults = []
    seen = set()
    for segment in str(raw or "").split(","):
        piece = segment.strip()
        if not piece:
            if segment or str(raw or "").count(","):
                faults.append("an empty segment, usually a trailing comma")
            continue
        if piece != piece.lower():
            faults.append("%r is not lower case; beta names are exact" % piece)
            piece = piece.lower()
        if " " in piece or "\t" in piece:
            faults.append("%r contains whitespace inside the name" % piece)
        if piece in seen:
            faults.append("%r is listed more than once" % piece)
            continue
        seen.add(piece)
        names.append(piece)
    # De-duplicated, order preserved, so the printed report is stable.
    return (tuple(names), tuple(dict.fromkeys(faults)))


def load_call_sites(raw):
    """{call site: raw header value}. Pure. Accepts JSON, a list or a string.

    Config lives in different shapes in different repositories and none of them
    is worth an argument, so all three are read and a value that cannot be
    parsed becomes one anonymous call site rather than an exception.
    """
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"(declared)": text}
    if isinstance(parsed, dict):
        return {str(k): str(v) for k, v in parsed.items()}
    if isinstance(parsed, list):
        return {"(declared)": ",".join(str(v) for v in parsed)}
    return {"(declared)": str(parsed)}


def levenshtein(a, b):
    """Edit distance between two strings. Pure.

    Written out rather than imported so the Python and Node.js versions rank
    candidates identically. A suggestion that differs between the two scripts
    is a suggestion nobody trusts.
    """
    a, b = str(a or ""), str(b or "")
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1,
                               current[j - 1] + 1,
                               previous[j - 1] + (0 if ca == cb else 1)))
        previous = current
    return previous[-1]


def near_matches(name, known=KNOWN_BETAS, limit=3, max_distance=6):
    """The closest documented names to a rejected string. Pure.

    Sorted by distance then alphabetically so the output does not reshuffle
    between runs. Empty when nothing is close, because a list of unrelated beta
    names is worse than no suggestion at all.
    """
    scored = []
    for candidate in known or ():
        distance = levenshtein(name, candidate)
        if distance <= max_distance:
            scored.append((distance, candidate))
    scored.sort()
    return tuple(candidate for _, candidate in scored[:limit])


def classify_probe(name, status, known=KNOWN_BETAS):
    """What one probe of one beta name means. Pure. Returns (state, detail).

    The 400 is deliberately not resolved into one cause. An invalid name and a
    beta this organization is not entitled to return the same documented
    message, and picking one would be an invention.
    """
    if status is None:
        return ("unreachable", "no response, so this name was not graded")
    status = int(status)
    documented = name in set(known or ())
    if status == 200:
        if documented:
            return ("accepted", "200, and the published enum lists it")
        return ("accepted-undocumented",
                "200, but the published enum does not list it. The endpoint "
                "accepts it, so the list is behind rather than the header "
                "being wrong")
    if status == 400:
        return ("rejected-typo" if near_matches(name, known) else "rejected-unknown",
                "400. Invalid, or a beta this organization is not entitled to; "
                "the API returns the same message for both")
    if status in (401, 403):
        return ("credentials",
                "%d, which is the key rather than the beta name" % status)
    return ("unexpected", "%d" % status)


def key_sets(payload):
    """(top-level keys, keys on the first data item). Pure.

    Two granularities because the documented graduation differences live at
    both: pagination cursors move at the top level, and expires_at appears on
    the individual objects.
    """
    body = payload if isinstance(payload, dict) else {}
    top = tuple(sorted(str(k) for k in body.keys()))
    data = body.get("data")
    first = data[0] if isinstance(data, list) and data else None
    item = tuple(sorted(str(k) for k in first.keys())) if isinstance(first, dict) else ()
    return (top, item)


def shape_delta(with_header, without_header):
    """Which keys differ between two bodies. Pure.

    {"top": (only_with, only_without), "item": (only_with, only_without)}.
    Sets rather than a diff of values: a beta header changes which fields exist,
    and comparing values would report every id and timestamp as a difference.
    """
    w_top, w_item = key_sets(with_header)
    n_top, n_item = key_sets(without_header)
    return {
        "top": (tuple(sorted(set(w_top) - set(n_top))),
                tuple(sorted(set(n_top) - set(w_top)))),
        "item": (tuple(sorted(set(w_item) - set(n_item))),
                 tuple(sorted(set(n_item) - set(w_item)))),
    }


def graduation_verdict(name, deltas):
    """Grade one accepted name by response shape. Pure. Returns (state, detail).

    deltas: {path: shape_delta(...)}. An identical pair is reported as a limit
    of the test rather than as a clean bill of health, because only some betas
    change a readable GET and this script can read exactly two of them.
    """
    changed = []
    for path in sorted(deltas or {}):
        delta = (deltas or {})[path] or {}
        if any(delta.get(scope, ((), ()))[side]
               for scope in ("top", "item") for side in (0, 1)):
            changed.append(path)
    if changed:
        return ("pinned-to-beta-shape",
                "accepted, and the response differs with and without it on: "
                + ", ".join(changed))
    return ("no-visible-difference",
            "same keys with and without it on the endpoints this script can "
            "read, which is not evidence that the header does nothing")


def conflicting(names):
    """[(a, b)] documented pairs present together. Pure."""
    have = set(str(n).strip().lower() for n in names or ())
    return [pair for pair in CONFLICTS if have.issuperset(pair)]


def repair_lines(state, name=None, matches=(), deltas=None):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state == "rejected-typo":
        return ["replace it with %s, then re-run this probe."
                % (matches[0] if matches else "the documented name"),
                "if the spelling is already exact, the other cause is "
                "entitlement: the same 400 is returned for a beta this "
                "organization does not have access to."]
    if state == "rejected-unknown":
        return ["nothing in the published enum is close to %r. Read the beta "
                "headers reference for the current name, and check entitlement "
                "before assuming it is a typo." % str(name)]
    if state == "pinned-to-beta-shape":
        lines = ["the beta graduated. The header is optional now and it is not "
                 "inert: it holds this client on the response shape it shipped "
                 "with. Read the migration notes before dropping it."]
        for path in sorted(deltas or {}):
            delta = (deltas or {})[path] or {}
            only_with, only_without = delta.get("top", ((), ()))
            if only_with:
                lines.append("%s top-level keys only with the header: %s"
                             % (path, ", ".join(only_with)))
            if only_without:
                lines.append("%s top-level keys only without it: %s"
                             % (path, ", ".join(only_without)))
            i_with, i_without = delta.get("item", ((), ()))
            if i_with:
                lines.append("%s item keys only with the header: %s"
                             % (path, ", ".join(i_with)))
            if i_without:
                lines.append("%s item keys only without it: %s"
                             % (path, ", ".join(i_without)))
        return lines
    if state == "conflicting-pair":
        return ["on memory store endpoints the first replaces the second. "
                "Sending both returns 400. Send agent-memory-2026-07-22 alone "
                "there and keep managed-agents-2026-04-01 for the agent, "
                "session and environment endpoints."]
    if state == "malformed-header":
        return ["multiple betas go in one comma separated header. Rebuild the "
                "string from a list rather than concatenating, and note that "
                "repeating a --beta flag on the CLI keeps only the first."]
    return []


def get(session, path, headers=None, params=None, timeout=60):
    """One GET. Returns (status, parsed body or None). Never raises on a 4xx.

    A 400 is the expected answer to half the probes here and is the most
    informative result the script can get, so it is data rather than an error.
    """
    try:
        r = session.get(API + path, headers=headers or {},
                        params=params or {}, timeout=timeout)
    except requests.RequestException as exc:
        log.debug("GET %s failed: %s", path, exc)
        return (None, None)
    try:
        return (r.status_code, r.json())
    except ValueError:
        return (r.status_code, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--beta", action="append", default=[],
                    help="a beta name your code sends (repeatable)")
    ap.add_argument("--skip-shape-diff", action="store_true",
                    help="probe validity only and do not compare responses")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a workspace key. This script only "
                  "issues GET requests")
        return 2

    call_sites = load_call_sites(os.environ.get("ANTHROPIC_BETA_HEADERS"))
    if args.beta:
        call_sites.setdefault("(command line)", ",".join(args.beta))
    if not call_sites:
        log.error("nothing to grade. Set ANTHROPIC_BETA_HEADERS to a JSON map "
                  "of call site to header value, or pass --beta")
        return 2

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION})

    findings = 0
    distinct = []
    for site in sorted(call_sites):
        names, faults = split_betas(call_sites[site])
        for name in names:
            if name not in distinct:
                distinct.append(name)
        for fault in faults:
            log.warning("%-20s %s sends %s", "malformed-header", site, fault)
            findings += 1
        if faults:
            for line in repair_lines("malformed-header"):
                log.warning("  repair: %s", line)
        for pair in conflicting(names):
            log.warning("%-20s %s sends %s with %s", "conflicting-pair", site,
                        pair[0], pair[1])
            for line in repair_lines("conflicting-pair"):
                log.warning("  repair: %s", line)
            findings += 1

    log.info("%d distinct beta string(s) across %d call site(s)",
             len(distinct), len(call_sites))

    for name in distinct:
        status, _ = get(session, "/models",
                        headers={"anthropic-beta": name}, params={"limit": 1})
        state, detail = classify_probe(name, status)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-20s %s: %s", state, name, detail)
        matches = near_matches(name) if state.startswith("rejected") else ()
        if matches:
            emit("  closest documented names: %s", ", ".join(matches))
        for line in repair_lines(state, name, matches):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1
            continue
        if state not in ("accepted", "accepted-undocumented") or args.skip_shape_diff:
            continue

        deltas = {}
        for path in DIFF_PATHS:
            with_status, with_body = get(session, path,
                                         headers={"anthropic-beta": name},
                                         params={"limit": 1})
            without_status, without_body = get(session, path, params={"limit": 1})
            if with_status != 200 or without_status != 200:
                continue
            deltas[path] = shape_delta(with_body, without_body)
        if not deltas:
            log.info("  neither listing was readable, so no shape comparison "
                     "was made for this name")
            continue
        state, detail = graduation_verdict(name, deltas)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-20s %s: %s", state, name, detail)
        for line in repair_lines(state, name, (), deltas):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
