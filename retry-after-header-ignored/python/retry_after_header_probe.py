"""Prove that retry-after can reach your client before you need it.

Read only, and deliberately small: one GET /v1/models per path. This script
will not drive traffic into a 429 in order to photograph one. Provoking the
failure you are investigating is not a diagnostic; on a saturated organization
it is a second outage, and on a healthy one it spends capacity that belongs to
production.

retry-after appears only on a 429, so its class is probed instead. The rate
limit triples arrive on every response, are added by the same layer, and are
forwarded or dropped by the same middlebox rules. If they survive the path on a
200, the wait instruction survives it on a 429.

Two paths, because one cannot attribute a loss: the same call straight at the
provider and through the base URL the application is configured with. Only the
-limit- values are compared, because remaining and reset are supposed to move
between two calls a second apart.
"""
import argparse
import datetime as dt
import email.utils
import logging
import os
import re
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("retry_after_header_probe")

DIRECT = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
}

# The headers documented as arriving on every response. These are the canary:
# retry-after belongs to the same family and is added by the same layer, so a
# path that keeps these keeps it too.
REQUIRED = {
    "anthropic": (
        "anthropic-ratelimit-requests-limit",
        "anthropic-ratelimit-requests-remaining",
        "anthropic-ratelimit-requests-reset",
        "anthropic-ratelimit-input-tokens-limit",
        "anthropic-ratelimit-input-tokens-remaining",
        "anthropic-ratelimit-input-tokens-reset",
        "anthropic-ratelimit-output-tokens-limit",
        "anthropic-ratelimit-output-tokens-remaining",
        "anthropic-ratelimit-output-tokens-reset",
        "anthropic-ratelimit-tokens-limit",
        "anthropic-ratelimit-tokens-remaining",
        "anthropic-ratelimit-tokens-reset",
    ),
    "openai": (
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
    ),
}

# Present in some configurations only, so their absence is reported and never
# counted as a loss: the priority triples require a Priority Tier commitment,
# the project triples appear when a project ceiling applies, and retry-after
# itself is a 429 header that a healthy probe must not expect to see.
OPTIONAL = {
    "anthropic": ("retry-after", "request-id", "anthropic-workspace-id",
                  "anthropic-priority-input-tokens-limit",
                  "anthropic-priority-output-tokens-limit"),
    "openai": ("retry-after", "x-request-id",
               "x-ratelimit-limit-project-tokens",
               "x-ratelimit-remaining-project-tokens",
               "x-ratelimit-reset-project-tokens"),
}

SKEW_SECONDS = 5.0

FINDINGS = ("headers-stripped", "headers-rewritten", "headers-absent",
            "reset-in-the-past", "clock-skew")

DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
UNIT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def lower_headers(headers):
    """{lowercase name: value}. Pure.

    HTTP header names are case-insensitive and middleboxes rewrite their casing
    freely, so a comparison that does not normalise reports a stripped header
    every time a proxy prefers title case.
    """
    out = {}
    for key, value in dict(headers or {}).items():
        out[str(key).strip().lower()] = str(value)
    return out


def missing(headers, provider):
    """Required header names absent from this response. Pure. Sorted."""
    present = lower_headers(headers)
    return sorted(n for n in REQUIRED.get(provider, ()) if n not in present)


def compare(direct, gateway, provider):
    """{header: (direct, gateway, state)} across two paths. Pure.

    States: intact, stripped, added, rewritten, absent-both. Only -limit- values
    are compared for equality. remaining and reset are supposed to differ
    between two calls made a second apart, and comparing them would make every
    healthy path look rewritten.
    """
    left = lower_headers(direct)
    right = lower_headers(gateway)
    names = set(REQUIRED.get(provider, ())) | set(OPTIONAL.get(provider, ()))
    names |= {n for n in list(left) + list(right)
              if "ratelimit" in n or n == "retry-after"}
    out = {}
    for name in sorted(names):
        a, b = left.get(name), right.get(name)
        if a is None and b is None:
            state = "absent-both"
        elif a is not None and b is None:
            state = "stripped"
        elif a is None and b is not None:
            state = "added"
        elif "-limit" in name and a != b:
            state = "rewritten"
        else:
            state = "intact"
        out[name] = (a, b, state)
    return out


def parse_reset(value):
    """(kind, seconds) for a reset header. Pure.

    kind is "absolute" with a POSIX timestamp, "duration" with a count of
    seconds, or "unknown" with None. Saying which it got matters: a duration
    needs no clock and an instant needs two clocks to agree.
    """
    text = str(value or "").strip()
    if not text:
        return ("unknown", None)
    try:
        stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return ("absolute", stamp.timestamp())
    except ValueError:
        pass
    parts = DURATION.findall(text)
    if parts and re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|h|m|s))+", text):
        return ("duration", sum(float(n) * UNIT[u] for n, u in parts))
    try:
        return ("duration", float(text))
    except ValueError:
        return ("unknown", None)


def clock_skew(date_header, local_epoch):
    """local clock minus the server's date header, in seconds. Pure.

    None when the header is missing or unparseable. Compared against the
    server's own clock rather than a third source, because the only agreement
    that matters is between this client and the API answering it.
    """
    text = str(date_header or "").strip()
    if not text:
        return None
    try:
        stamp = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return float(local_epoch) - stamp.timestamp()


def stale_resets(headers, provider, server_epoch):
    """[(header, seconds_in_the_past)] for absolute resets already elapsed. Pure."""
    present = lower_headers(headers)
    out = []
    for name in REQUIRED.get(provider, ()):
        if not name.endswith("-reset"):
            continue
        kind, value = parse_reset(present.get(name))
        if kind == "absolute" and value is not None and value < server_epoch:
            out.append((name, server_epoch - value))
    out.sort(key=lambda r: (-r[1], r[0]))
    return out


def verdict(comparison, direct_missing, gateway_used, skew, stale):
    """Classify one provider's probe. Pure. Returns (state, detail).

    Ordered so a transport failure is reported before a clock one: a stripped
    header makes the clock question moot, since there is nothing to compute a
    sleep from in the first place.
    """
    comparison = comparison or {}
    states = [s for _, _, s in comparison.values()]
    stripped = [n for n, (_, _, s) in comparison.items() if s == "stripped"]
    rewritten = [n for n, (_, _, s) in comparison.items() if s == "rewritten"]
    total = len(REQUIRED_ANY(comparison))

    if direct_missing and not gateway_used:
        return ("headers-absent",
                "%d required rate limit header(s) did not arrive at all, and "
                "there is no gateway configured to blame for it"
                % len(direct_missing))
    if stripped:
        return ("headers-stripped",
                "%d of %d rate limit header(s) do not survive the gateway"
                % (len(stripped), max(total, len(stripped))))
    if rewritten:
        return ("headers-rewritten",
                "%d limit value(s) differ between the two paths, so something "
                "is generating headers rather than forwarding them"
                % len(rewritten))
    if direct_missing:
        return ("headers-absent",
                "%d required rate limit header(s) are absent on both paths"
                % len(direct_missing))
    if stale:
        return ("reset-in-the-past",
                "%s is already %.0fs in the past by the server's own clock"
                % (stale[0][0], stale[0][1]))
    if skew is not None and abs(skew) > SKEW_SECONDS:
        return ("clock-skew",
                "local clock is %.0fs %s the server's date header"
                % (abs(skew), "behind" if skew < 0 else "ahead of"))
    intact = states.count("intact")
    return ("headers-intact",
            "%d rate limit header(s) present and consistent across every path "
            "checked" % intact)


def REQUIRED_ANY(comparison):
    """The header names in a comparison that are required somewhere. Pure."""
    names = set()
    for provider, required in REQUIRED.items():
        names |= {n for n in required if n in (comparison or {})}
    return names


def repair_lines(state, provider="", names=()):
    """The repair for one verdict. Pure. Printed, never performed."""
    names = list(names or [])
    if state == "headers-stripped":
        return ["retry-after travels with these. A path that drops them on a 200 "
                "drops the wait instruction on a 429, and your backoff falls "
                "back to a constant that retries into an empty bucket.",
                "add these names to the response header allowlist on the "
                "gateway: " + (", ".join(names[:6]) or "(none recorded)")
                + (" ..." if len(names) > 6 else "")]
    if state == "headers-rewritten":
        return ["a limit value that differs between two paths a second apart is "
                "not a live number. Find the layer caching or synthesising "
                "responses and make it forward the origin's headers unchanged.",
                "this state is more dangerous than stripping, because the client "
                "believes the numbers it is given and has no way to tell."]
    if state == "headers-absent":
        return ["nothing arrived on any path checked, so this is not attributable "
                "yet. Re-run with the gateway base URL set, and confirm the "
                "credential and endpoint are the ones production uses."]
    if state == "reset-in-the-past":
        return ["a reset instant already in the past makes any sleep computed "
                "from it a no-op, so the client retries immediately and 429s "
                "again. Prefer retry-after, which is relative."]
    if state == "clock-skew":
        if provider == "anthropic":
            return ["anthropic reset values are RFC 3339 instants, so a sleep "
                    "computed from one is only as good as clock agreement. Fix "
                    "time sync on this host, or use retry-after instead, which "
                    "is relative and immune to skew.",
                    "the same skew affects any log correlation you do against "
                    "these timestamps, which is usually how it is finally noticed."]
        return ["this provider returns reset values as durations, so backoff is "
                "unaffected, but the skew will still misalign every log line you "
                "correlate against the API's timestamps."]
    return []


def probe(url, headers, timeout=30):
    """One GET. Returns (status, headers, note). Never retried, never repeated."""
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return (None, {}, "request failed: %s" % exc)
    note = ""
    if r.status_code == 429:
        # Not provoked, and not retried. If one happens to arrive it is the
        # direct observation this script cannot go looking for.
        note = ("a 429 arrived on its own. retry-after came back as %r"
                % r.headers.get("retry-after"))
    elif r.status_code in (401, 403):
        note = "%d: the credential cannot read this path" % r.status_code
    return (r.status_code, dict(r.headers), note)


def audit(provider, key, base_url):
    direct_base = DIRECT[provider]
    auth = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
            if provider == "anthropic" else {"Authorization": "Bearer " + key})
    auth["User-Agent"] = "retry-after-header-probe/1.0"

    log.info("%s: direct %s, %s", provider,
             direct_base.split("//")[-1].split("/")[0],
             "gateway " + base_url.split("//")[-1].split("/")[0]
             if base_url else "no gateway configured")

    status, direct_headers, note = probe(direct_base + "/models", auth)
    if note:
        log.info("  direct: %s", note)
    gateway_headers = {}
    if base_url:
        time.sleep(1)
        _, gateway_headers, gnote = probe(base_url.rstrip("/") + "/models", auth)
        if gnote:
            log.info("  gateway: %s", gnote)

    comparison = compare(direct_headers, gateway_headers or direct_headers, provider)
    direct_missing = missing(direct_headers, provider)
    skew = clock_skew(lower_headers(direct_headers).get("date"), time.time())
    server_epoch = time.time() - (skew or 0.0)
    stale = stale_resets(direct_headers, provider, server_epoch)

    state, detail = verdict(comparison, direct_missing, bool(base_url), skew, stale)
    emit = log.warning if state in FINDINGS else log.info
    emit("%-21s %s", state, detail)

    stripped = [n for n, (_, _, s) in comparison.items() if s == "stripped"]
    for name in stripped[:6]:
        emit("  stripped   %s", name)
    for name, (a, _b, s) in sorted(comparison.items()):
        if s == "intact" and name.endswith("-limit") and a:
            emit("  intact     %-42s %s", name, a)
    for name, seconds in stale[:3]:
        emit("  stale      %s, %.0fs in the past", name, seconds)
    for line in repair_lines(state, provider, stripped):
        emit("  repair: %s", line)
    return 1 if state in FINDINGS else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--anthropic-base-url", default=os.environ.get("ANTHROPIC_BASE_URL"))
    ap.add_argument("--openai-base-url", default=os.environ.get("OPENAI_BASE_URL"))
    args = ap.parse_args()

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not anthropic_key and not openai_key:
        log.error("set ANTHROPIC_API_KEY, OPENAI_API_KEY, or both, and set the "
                  "matching base URL if production reaches the API through a "
                  "gateway")
        return 2

    findings = 0
    if anthropic_key:
        findings += audit("anthropic", anthropic_key, args.anthropic_base_url)
    if openai_key:
        findings += audit("openai", openai_key, args.openai_base_url)

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
