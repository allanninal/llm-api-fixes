"""Name which Anthropic rate limiter is binding, instead of catching 429.

Read only. Two GET requests and nothing else. ANTHROPIC_API_KEY is a workspace
key used for a single probe against /v1/models, which generates no tokens and
bills nothing; ANTHROPIC_ADMIN_KEY is an Admin API key (sk-ant-admin...) used
for the configured limits, because /v1/organizations/* rejects a workspace key.

Anthropic has no read-only tier on the data plane: the same workspace key that
reads /v1/models could send a message. This script is trusted not to rather
than prevented from it, so it makes exactly one call with that credential.

Nothing here provokes a 429. Draining a production bucket to inspect the error
is an outage you scheduled on purpose.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_limiter_identify")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# The three limiters that empty independently. "tokens" is deliberately not in
# this tuple: it is not a fourth bucket, it is a report on whichever of the two
# token buckets is currently most restrictive.
NAMED = ("requests", "input-tokens", "output-tokens")
AGGREGATE = "tokens"

LIMITER_TYPES = ("requests_per_minute", "input_tokens_per_minute",
                 "output_tokens_per_minute")

FINDINGS = ("disagreement", "aggregate-unmatched", "headers-missing")


def parse_count(value):
    """Read a limit or remaining header as an integer. Pure, None if unreadable.

    None and zero must stay distinct. Zero means the bucket is empty; None means
    nothing told us, and reporting a stripped header as an empty limiter sends
    somebody chasing a throttle that is not there.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("_", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def read_triples(headers):
    """Parse the anthropic-ratelimit-* triples off one response. Pure.

    Case-insensitive, because a proxy that rewrites header casing should not be
    able to turn a working probe into a report of missing headers.
    """
    lower = {}
    for name, value in dict(headers or {}).items():
        lower[str(name).strip().lower()] = value

    out = {}
    for name in NAMED + (AGGREGATE,):
        limit_h = "anthropic-ratelimit-%s-limit" % name
        remaining_h = "anthropic-ratelimit-%s-remaining" % name
        reset_h = "anthropic-ratelimit-%s-reset" % name
        if limit_h not in lower and remaining_h not in lower:
            continue
        reset = lower.get(reset_h)
        out[name] = {"limit": parse_count(lower.get(limit_h)),
                     "remaining": parse_count(lower.get(remaining_h)),
                     "reset": str(reset).strip() if reset is not None else None}
    return out


def seconds_until(value, now):
    """Seconds until an RFC 3339 reset stamp. Pure; the caller supplies now.

    Returns None when the stamp cannot be read rather than guessing. A reset
    window is a number a reader will act on, and half-parsing one is worse than
    printing that it was unreadable.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for suffix in ("Z", "z"):
        if text.endswith(suffix):
            text = text[:-1] + "+00:00"
            break
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (when - now).total_seconds()


def share_left(triple):
    """remaining / limit for one triple, or None. Pure."""
    if not isinstance(triple, dict):
        return None
    limit = triple.get("limit")
    remaining = triple.get("remaining")
    if limit is None or remaining is None or limit <= 0:
        return None
    return max(0.0, min(1.0, remaining / float(limit)))


def mirrors(parsed):
    """Which named token limiter the aggregate triple is reporting. Pure.

    anthropic-ratelimit-tokens-* is documented as the most restrictive token
    limit currently in effect, so its ceiling equals the input ceiling or the
    output ceiling. Matching it back is the platform naming the binding bucket
    for you; nothing else in the response does that.
    """
    aggregate = (parsed or {}).get(AGGREGATE) or {}
    limit = aggregate.get("limit")
    if limit is None:
        return "no-aggregate"
    matched = []
    for name in ("input-tokens", "output-tokens"):
        other = (parsed or {}).get(name) or {}
        if other.get("limit") is not None and other.get("limit") == limit:
            matched.append(name)
    if len(matched) == 2:
        return "both"
    if len(matched) == 1:
        return matched[0]
    return "unmatched"


def emptiest(parsed):
    """The named bucket with the least left. Pure. Returns (name, share).

    The aggregate is excluded on purpose: it duplicates one of the named
    buckets, and letting it compete would report the same limiter twice under
    two names and hide whichever one it is not mirroring.
    """
    best = None
    for name in NAMED:
        share = share_left((parsed or {}).get(name) or {})
        if share is None:
            continue
        if best is None or share < best[1]:
            best = (name, share)
    return best


def verdict(parsed):
    """Say which limiter is binding, and when the two answers disagree. Pure."""
    if not parsed:
        return ("headers-missing",
                "no anthropic-ratelimit-* headers reached this process, so a "
                "429 here would arrive with nothing to classify it by and "
                "retry-after would be missing too")
    scarce = emptiest(parsed)
    if scarce is None:
        return ("unreadable",
                "the named triples arrived without a usable limit and "
                "remaining pair, so there is no ratio to compare")

    name, share = scarce
    shape = "%s is the emptiest named bucket at %.0f%% remaining" % (name, share * 100)
    mirror = mirrors(parsed)

    if mirror == "no-aggregate":
        return ("no-aggregate",
                "%s, and the aggregate anthropic-ratelimit-tokens triple is "
                "absent, so the platform's own view of the most restrictive "
                "token limit is not available on this response." % shape)
    if mirror == "unmatched":
        return ("aggregate-unmatched",
                "%s, but the aggregate token ceiling matches neither the input "
                "nor the output ceiling. A third and lower limit is in effect: "
                "a workspace override, or a different limiter group than the "
                "one this probe touched." % shape)
    if mirror == "both":
        return ("identified",
                "%s, and the aggregate ceiling equals both token ceilings, so "
                "input and output share a number here and only the remaining "
                "counters tell them apart." % shape)
    if mirror == name:
        return ("identified",
                "%s, and the aggregate ceiling mirrors %s. The tightest ceiling "
                "and the emptiest bucket are the same limiter." % (shape, mirror))
    return ("disagreement",
            "%s, while the aggregate ceiling mirrors %s. The tightest ceiling "
            "and the emptiest bucket are different limiters, so a handler that "
            "records only one of them will name the wrong cause."
            % (shape, mirror))


def configured(payload):
    """Fold GET /v1/organizations/rate_limits into {model_group: {type: value}}. Pure.

    A limiter type missing from a group's limits[] is not unlimited: it
    inherits. It is recorded as None and printed as unpublished, never as
    absent, because "no number" read as "no ceiling" is how a team convinces
    itself it has headroom it was never granted.
    """
    out = {}
    for entry in (payload or {}).get("data") or []:
        group = str(entry.get("model_group") or "").strip()
        if not group:
            continue
        row = out.setdefault(group, dict.fromkeys(LIMITER_TYPES))
        for limit in entry.get("limits") or []:
            kind = str(limit.get("type") or "").strip()
            if kind not in row:
                continue
            try:
                row[kind] = int(limit.get("value"))
            except (TypeError, ValueError):
                row[kind] = None
    return out


def log_headers(headers):
    """The header names a 429 handler should be recording. Pure.

    Built from what actually arrived rather than from a hardcoded list, so the
    printed repair does not tell a reader to log a header their gateway is
    stripping. This list is the whole output of the script that matters.
    """
    lower = set()
    for name in dict(headers or {}):
        lower.add(str(name).strip().lower())
    wanted = set()
    for name in NAMED + (AGGREGATE,):
        for suffix in ("limit", "remaining", "reset"):
            candidate = "anthropic-ratelimit-%s-%s" % (name, suffix)
            if candidate in lower:
                wanted.add(candidate)
    for extra in ("retry-after", "request-id", "anthropic-organization-id"):
        if extra in lower:
            wanted.add(extra)
    return sorted(wanted)


def probe(session):
    """One cheap real call with the workspace key. Generates nothing."""
    r = session.get(API + "/models", timeout=30)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: ANTHROPIC_API_KEY must be a "
                         "workspace or project key" % r.status_code)
    if r.status_code == 429:
        log.warning("the probe itself was rate limited; the headers below "
                    "describe the bucket that rejected it")
        return r.headers
    r.raise_for_status()
    return r.headers


def admin_limits(admin_key):
    """GET the configured per-model-group limits. Returns {} if no admin key."""
    if not admin_key:
        return {}
    s = requests.Session()
    s.headers.update({"x-api-key": admin_key, "anthropic-version": VERSION})
    r = s.get(API + "/organizations/rate_limits", timeout=60)
    if r.status_code in (401, 403):
        log.warning("%d from the Admin API: /v1/organizations/* needs an Admin "
                    "key (sk-ant-admin...). Continuing on headers alone.",
                    r.status_code)
        return {}
    r.raise_for_status()
    return configured(r.json())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show-all", action="store_true",
                    help="also print every triple, not only the verdict")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY (a workspace key) for the probe")
        return 2

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION})

    headers = probe(session)
    parsed = read_triples(headers)
    state, detail = verdict(parsed)
    line = "%-20s %s" % (state, detail)
    if state in FINDINGS:
        log.warning(line)
    else:
        log.info(line)

    now = dt.datetime.now(dt.timezone.utc)
    if args.show_all:
        for name in sorted(parsed):
            triple = parsed[name]
            until = seconds_until(triple.get("reset"), now)
            log.info("  %-14s limit %s, remaining %s, resets %s", name,
                     triple.get("limit"), triple.get("remaining"),
                     "in %.0fs" % until if until is not None else "unreadable")

    groups = admin_limits(os.environ.get("ANTHROPIC_ADMIN_KEY"))
    for group in sorted(groups):
        row = groups[group]
        log.info("  %-24s rpm %s  itpm %s  otpm %s", group,
                 row["requests_per_minute"] if row["requests_per_minute"] is not None
                 else "unpublished",
                 row["input_tokens_per_minute"] if row["input_tokens_per_minute"] is not None
                 else "unpublished",
                 row["output_tokens_per_minute"] if row["output_tokens_per_minute"] is not None
                 else "unpublished")
    if not groups:
        log.info("  no configured limits read; set ANTHROPIC_ADMIN_KEY to name "
                 "the ceilings per model group rather than only the probe's")

    names = log_headers(headers)
    if names:
        log.warning("  repair: record these on every 429 instead of catching a "
                    "broad status error: %s", ", ".join(names))
        log.warning("  repair: branch before sleeping. No retry-after plus "
                    "error.details.error_code of enforced_spend_limit_reached "
                    "is a billing stop, not a throttle, and will not clear.")
    else:
        log.warning("  repair: no rate-limit headers arrived at all. Check the "
                    "proxy or gateway in front of api.anthropic.com and let "
                    "the anthropic-ratelimit-* and retry-after headers through.")

    return 1 if state in FINDINGS else 0


if __name__ == "__main__":
    sys.exit(main())
