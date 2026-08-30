"""Report how much OpenAI rate-limit headroom is left, before anything 429s.

Read only. One GET request and nothing else: OPENAI_API_KEY should be a project
key set to Read Only. GET /v1/models consumes no inference quota and carries the
same x-ratelimit-* header set as a completion, which is the whole trick, because
OpenAI has no endpoint that returns remaining quota on request.

The repair is printed, never performed. Raising a project rate limit is a write
call against a ceiling your colleagues share.

This script never tries to provoke a 429. Draining a production token bucket to
see what the error looks like is an outage you caused on purpose.
"""
import argparse
import logging
import os
import re
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_rate_limit_headroom")

API = "https://api.openai.com/v1"

# The dimensions OpenAI reports. The project-scoped pair is present only when the
# project carries its own limit, and when it is present it is usually the lower
# of the two, which makes it the one that actually binds.
DIMENSIONS = ("requests", "tokens", "project-requests", "project-tokens")

# The reset headers are Go duration strings: "6m0s", "500ms", "1h2m3s". Ordered
# so that "ms" is tried before "m", because the other way round parses 500ms as
# 500 minutes and reports eight hours of pressure that does not exist.
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|us|ns|h|m|s)")
_UNITS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}

FINDINGS = ("exhausted", "near-exhaustion")


def header_names(dimension):
    """The limit/remaining/reset header triple for one dimension. Pure."""
    return ("x-ratelimit-limit-" + dimension,
            "x-ratelimit-remaining-" + dimension,
            "x-ratelimit-reset-" + dimension)


def parse_count(value):
    """Read a limit or remaining header as an integer. Pure.

    Returns None rather than zero when the value is missing or unreadable.
    Zero is a real and important state here: it means the bucket is empty. A
    parser that folds "absent" into "empty" reports a stripped header as an
    exhausted limiter and sends somebody to the wrong console.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("_", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_reset(value):
    """Read a reset header as seconds. Pure. Returns None if unreadable.

    The whole string has to be consumed. A partial match on something like
    "60 seconds" would return 60.0 from a format this parser does not actually
    understand, and a reset window is exactly the number a reader will act on.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    parts = _DURATION.findall(text)
    if not parts:
        return None
    if "".join(a + b for a, b in parts) != text:
        return None
    return sum(float(a) * _UNITS[b] for a, b in parts)


def triples(headers):
    """Parse the x-ratelimit-* triples off one response. Pure.

    Matched case-insensitively because gateways and proxies rewrite header
    casing freely, and a dict keyed on the exact casing OpenAI sends is how a
    working probe starts reporting no headers the day a load balancer changes.
    """
    lower = {}
    for name, value in dict(headers or {}).items():
        lower[str(name).strip().lower()] = value

    out = {}
    for dimension in DIMENSIONS:
        limit_h, remaining_h, reset_h = header_names(dimension)
        if limit_h not in lower and remaining_h not in lower:
            continue
        out[dimension] = {"limit": parse_count(lower.get(limit_h)),
                          "remaining": parse_count(lower.get(remaining_h)),
                          "reset": parse_reset(lower.get(reset_h))}
    return out


def headroom(triple):
    """remaining / limit for one dimension, or None if it cannot be computed. Pure."""
    if not isinstance(triple, dict):
        return None
    limit = triple.get("limit")
    remaining = triple.get("remaining")
    if limit is None or remaining is None or limit <= 0:
        return None
    return max(0.0, min(1.0, remaining / float(limit)))


def verdict(dimension, triple, floor=0.2):
    """Classify one dimension. Pure. Returns (state, detail)."""
    share = headroom(triple)
    if share is None:
        return ("unreadable",
                "the %s triple arrived without a usable limit and remaining "
                "pair, so there is no ratio to read" % dimension)

    remaining = triple.get("remaining")
    limit = triple.get("limit")
    reset = triple.get("reset")
    window = ("resets in %.0fs" % reset) if reset is not None else "no readable reset"
    shape = "%d of %d left (%.0f%%), %s" % (remaining, limit, share * 100, window)

    if remaining == 0:
        return ("exhausted",
                "%s. This bucket is empty now, so the next call in this window "
                "is a 429 no matter how small it is." % shape)
    if share < floor:
        return ("near-exhaustion",
                "%s. Under the %.0f%% floor, which means the next traffic spike "
                "converts this into a 429." % (shape, floor * 100))
    return ("headroom", shape + ".")


def binding(parsed):
    """The dimension with the least headroom left. Pure. Returns (name, share).

    Token headroom and request headroom empty independently, so the mean of the
    two is a number about nothing. The minimum is the one that produces the 429
    and the only one worth putting in a report.
    """
    best = None
    for dimension in sorted(parsed or {}):
        share = headroom(parsed[dimension])
        if share is None:
            continue
        if best is None or share < best[1]:
            best = (dimension, share)
    return best


def scope_note(parsed):
    """Which scope owns the real ceiling, per dimension. Pure.

    Returns a list of (owner, dimension, binding_limit, other_limit). The
    project-scoped headers are present only when the project carries its own
    limit; when it is lower than the organization's, reading the org triple and
    concluding there is room is the exact mistake this function exists to stop.
    """
    out = []
    for dimension in ("requests", "tokens"):
        org = (parsed or {}).get(dimension) or {}
        project = (parsed or {}).get("project-" + dimension) or {}
        org_limit = org.get("limit")
        project_limit = project.get("limit")
        if org_limit is None or project_limit is None:
            continue
        if project_limit < org_limit:
            out.append(("project", dimension, project_limit, org_limit))
        elif org_limit < project_limit:
            out.append(("organization", dimension, org_limit, project_limit))
        else:
            out.append(("equal", dimension, project_limit, org_limit))
    return out


def probe(session):
    """One cheap real call. GET only, and it consumes no inference quota."""
    r = session.get(API + "/models", timeout=30)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI: OPENAI_API_KEY is not a valid key")
    if r.status_code == 429:
        # Worth saying plainly: the headers are still on this response, and a
        # 429 here is about the model-list endpoint, not about inference.
        log.warning("the probe itself was rate limited; the headers below "
                    "describe the bucket that rejected it")
        return r.headers
    r.raise_for_status()
    return r.headers


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--floor", type=float, default=0.2,
                    help="headroom share below which a dimension is a finding "
                         "(default 0.2)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print dimensions with plenty of headroom")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY (a project key set to Read Only)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    parsed = triples(probe(session))
    if not parsed:
        log.warning("headers-missing    no x-ratelimit-* headers reached this "
                    "process at all")
        log.warning("  This is not a clean bill of health. Something between "
                    "you and OpenAI is stripping response headers, so you have "
                    "no forward-looking signal and no Retry-After on the 429 "
                    "when it arrives.")
        log.warning("  repair: check the proxy, gateway or LLM router in front "
                    "of api.openai.com and allow the x-ratelimit-* and "
                    "retry-after headers through unmodified")
        return 1

    checked = 0
    bad = 0
    for dimension in sorted(parsed):
        state, detail = verdict(dimension, parsed[dimension], args.floor)
        checked += 1
        line = "%-16s %-18s %s" % (state, dimension, detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
        elif state == "unreadable":
            log.warning(line)
        elif args.show_all:
            log.info(line)

    scarcest = binding(parsed)
    if scarcest:
        log.info("binding dimension: %s, at %.0f%% of its ceiling",
                 scarcest[0], scarcest[1] * 100)

    for owner, dimension, low, high in scope_note(parsed):
        if owner == "project":
            log.warning("  note: the project ceiling binds for %s (%d against "
                        "an org %d), so org headroom is not your headroom",
                        dimension, low, high)
        elif owner == "organization":
            log.info("  note: the org ceiling binds for %s (%d against a "
                     "project %d)", dimension, low, high)

    if bad:
        log.warning("  repair: request a usage tier increase, or pace the "
                    "client with a token bucket sized to the limit above so "
                    "bursts are spread across the window instead of rejected")
        log.warning("  repair: to raise the project ceiling instead, an admin "
                    "can call POST /v1/organization/projects/{project_id}"
                    "/rate_limits/{rate_limit_id}. That is a write against a "
                    "limit your colleagues share, so it is printed, not run.")

    log.info("%d dimension(s) read, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
