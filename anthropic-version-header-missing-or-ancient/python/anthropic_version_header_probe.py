"""Probe the anthropic-version header three ways, direct and via a gateway.

Read only. Every request is a GET of /v1/models, which lists model metadata,
generates no tokens and bills nothing. Nothing here sends a message, and a 400
from this endpoint costs exactly as little as a 200 -- which is the only reason
a deliberately failing probe is acceptable at all.

Three probes per host: no version header, the current 2023-06-01, and the
2023-01-01 initial release, plus every version string your own clients send,
declared on the command line because nothing in the API can read your source.

No single status is the finding. A required header is only proved required by
two probes that disagree about it, and a header injected or stripped in transit
is only visible by running the same matrix down two paths and diffing them.
"""
import argparse
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_version_header_probe")

API_PATH = "/v1/models"
DIRECT = "https://api.anthropic.com"

# The complete version history. Two entries, and there have never been more.
INITIAL = "2023-01-01"
CURRENT = "2023-06-01"
KNOWN = (INITIAL, CURRENT)

# The label for the probe that deliberately sends no version header. It is not
# a version string and is never sent as one; probe_headers() is where that is
# enforced, and there is a test that says so.
ABSENT = "(absent)"

FINDINGS = ("version-not-enforced", "current-rejected", "ancient-pinned",
            "unknown-version-pinned", "gateway-injects", "gateway-strips",
            "gateway-disagrees", "unreachable")

REPAIRS = {
    "version-not-enforced":
        "something on this path adds anthropic-version for you. Find it, then "
        "set the header in each client as well: a header the infrastructure "
        "supplies is a header your code does not have.",
    "gateway-injects":
        "set anthropic-version: 2023-06-01 in the client itself. A client that "
        "only works behind the gateway is one routing change from a 400 on "
        "every request.",
    "gateway-strips":
        "the gateway is removing or rewriting anthropic-version. Fix it there; "
        "a client cannot compensate for a header that does not survive the "
        "hop.",
    "gateway-disagrees":
        "the two paths do not behave the same. Read the gateway's request "
        "header policy before trusting either matrix as a description of what "
        "your clients send.",
    "current-rejected":
        "the current version probe did not return 200, so this is a credential "
        "or connectivity problem rather than a versioning one. Nothing else in "
        "this matrix can be trusted until it is.",
    "ancient-pinned":
        "move the pin to anthropic-version: 2023-06-01, and read your streaming "
        "code first: 2023-06-01 sends incremental named events and no "
        "data: [DONE].",
    "unknown-version-pinned":
        "only 2023-01-01 and 2023-06-01 have ever existed. Replace the string "
        "with 2023-06-01 rather than trying to make it work.",
}


def probe_headers(label):
    """The version header for one probe. Pure. Empty dict for ABSENT.

    Deliberately separate from the credential. The absent probe has to send no
    anthropic-version at all, and a function that merged the auth header in
    would make that hard to assert without handling a key in a test.
    """
    if label == ABSENT:
        return {}
    return {"anthropic-version": str(label).strip()}


def probe_labels(declared):
    """The ordered probe set. Pure. ABSENT, the two real versions, then yours.

    De-duplicated with order preserved so the printed matrix is stable between
    runs, and stripped so a trailing space in an environment variable does not
    become a fourth version that has never existed.
    """
    out = [ABSENT, CURRENT, INITIAL]
    for raw in declared or []:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def classify_status(label, status):
    """What one probe result means on its own. Pure. Returns (state, detail).

    On its own is the operative phrase. Nothing here is a verdict; the verdicts
    need two rows and live in host_verdict() and gateway_verdict().
    """
    if status is None:
        return ("unreachable", "no response at all from this host")
    status = int(status)
    if label == ABSENT:
        if status == 400:
            return ("enforced", "400 with no version header, which is correct")
        if status == 200:
            return ("not-enforced",
                    "200 with no version header, so something on this path is "
                    "supplying one for you")
        if status in (401, 403):
            return ("credentials",
                    "%d, so this probe says nothing about the version header"
                    % status)
        return ("unexpected", "%d with no version header" % status)
    if status == 200:
        if label == CURRENT:
            return ("accepted", "200, the current version")
        if label == INITIAL:
            return ("accepted-deprecated",
                    "200, but 2023-01-01 is deprecated and predates the named "
                    "SSE events")
        return ("accepted-unknown",
                "200 for a string that is not one of the two documented "
                "versions")
    if status in (401, 403):
        return ("credentials",
                "%d, which is the credential rather than the version" % status)
    if status in (400, 404, 410):
        return ("refused",
                "%d, this host will not serve that version" % status)
    return ("unexpected", "%d" % status)


def host_verdict(results):
    """Grade one host's whole matrix. Pure. Returns (state, detail).

    The current-version probe is the gate. If it is not a 200 then the key, the
    host or the network is the story and every other row is noise, so this
    returns early rather than reporting a header problem it cannot see.
    """
    results = dict(results or {})
    current = results.get(CURRENT)
    absent = results.get(ABSENT)
    if current is None:
        return ("unreachable",
                "the current version probe got no response, so nothing else on "
                "this host can be read")
    if int(current) in (401, 403):
        return ("current-rejected",
                "%d for anthropic-version: %s, which is a credential problem "
                "and not a versioning one" % (int(current), CURRENT))
    if int(current) != 200:
        return ("current-rejected",
                "%d for anthropic-version: %s, which should be 200"
                % (int(current), CURRENT))
    if absent is not None and int(absent) == 200:
        return ("version-not-enforced",
                "200 with no anthropic-version header at all. The header is "
                "documented as required, so a proxy, SDK or gateway on this "
                "path is adding it")
    return ("version-enforced",
            "400 without the header and 200 with %s, which is the shape a "
            "direct connection should have" % CURRENT)


def declared_findings(results, declared):
    """[(version, state, detail)] for the strings your clients send. Pure.

    Graded against the documented version history, not against the status code.
    A pin that works today and is deprecated is still a pin that is deprecated,
    and a script that only reported non-200s would wave both of these through.
    """
    results = dict(results or {})
    seen = set()
    out = []
    for raw in declared or []:
        text = str(raw or "").strip()
        if not text or text in seen or text == CURRENT:
            continue
        seen.add(text)
        status = results.get(text)
        suffix = ("" if status is None
                  else " (this host returns %d for it)" % int(status))
        if text == INITIAL:
            out.append((text, "ancient-pinned",
                        "2023-01-01 is the initial release and is deprecated. A "
                        "client pinned there does not get the 2023-06-01 SSE "
                        "format: incremental named events, and no "
                        "data: [DONE]" + suffix))
        else:
            out.append((text, "unknown-version-pinned",
                        "only 2023-01-01 and 2023-06-01 have ever existed, so "
                        "this string is a typo or an invention" + suffix))
    out.sort()
    return out


def gateway_verdict(direct, proxy):
    """Compare two hosts' matrices. Pure. Returns (state, detail).

    The only function here that looks at two hosts at once, and the only way a
    header rewritten in transit becomes visible: from the client it is a 200,
    from the API it is a valid request, and nothing but the diff shows it.
    """
    direct = dict(direct or {})
    proxy = dict(proxy or {})
    if not proxy:
        return ("no-gateway",
                "no gateway base URL was given, so nothing was compared. A "
                "header added in transit is invisible to a single host")
    d_absent, p_absent = direct.get(ABSENT), proxy.get(ABSENT)
    d_current, p_current = direct.get(CURRENT), proxy.get(CURRENT)
    if (d_absent is not None and p_absent is not None
            and int(d_absent) == 400 and int(p_absent) == 200):
        return ("gateway-injects",
                "the direct host 400s without the header and the gateway "
                "returns 200, so the gateway adds anthropic-version for you. "
                "Every client behind it is untested")
    if (d_current is not None and p_current is not None
            and int(d_current) == 200 and int(p_current) != 200):
        return ("gateway-strips",
                "anthropic-version: %s is accepted directly and returns %d "
                "through the gateway, so it is being stripped or rewritten in "
                "transit" % (CURRENT, int(p_current)))
    differing = sorted(label for label in set(direct) | set(proxy)
                       if direct.get(label) != proxy.get(label))
    if differing:
        return ("gateway-disagrees",
                "the two hosts return different statuses for: "
                + ", ".join(differing))
    return ("gateway-agrees",
            "both hosts return the same status for every probe, so nothing on "
            "the way is rewriting the header")


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed."""
    line = REPAIRS.get(state)
    if not line:
        return []
    if state in ("gateway-injects", "gateway-strips", "version-not-enforced"):
        return [line,
                "the durable fix is the official SDK, which sets "
                "anthropic-version on every request whether or not anything "
                "else does."]
    return [line]


def probe(session, base, key, label, timeout=30):
    """One GET. Returns a status code, or None when the host is unreachable.

    Never raises on a 4xx: a 400 is the expected answer to one of these probes
    and is the most informative result the script can get.
    """
    headers = {"x-api-key": key}
    headers.update(probe_headers(label))
    try:
        r = session.get(base.rstrip("/") + API_PATH, headers=headers,
                        params={"limit": 1}, timeout=timeout)
    except requests.RequestException as exc:
        log.debug("probe %s against %s failed: %s", label, base, exc)
        return None
    return r.status_code


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", action="append", default=[],
                    help="a version string your clients send (repeatable)")
    ap.add_argument("--gateway", default=os.environ.get("ANTHROPIC_BASE_URL"),
                    help="base URL of the proxy or gateway to compare against")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a workspace key. This script only "
                  "issues GET requests against %s", API_PATH)
        return 2

    declared = list(args.version)
    declared += [p.strip() for p in
                 (os.environ.get("ANTHROPIC_VERSIONS") or "").split(",")
                 if p.strip()]
    labels = probe_labels(declared)

    hosts = [("direct", DIRECT)]
    if args.gateway and args.gateway.rstrip("/") != DIRECT:
        hosts.append(("gateway", args.gateway))

    session = requests.Session()
    matrices = {}
    findings = 0

    for role, base in hosts:
        results = {}
        log.info("host %s", base)
        for label in labels:
            status = probe(session, base, key, label)
            results[label] = status
            state, detail = classify_status(label, status)
            emit = log.warning if state in ("not-enforced", "unreachable") else log.info
            emit("  %-13s %s  %-20s %s", label,
                 "---" if status is None else status, state, detail)
        matrices[role] = results

        state, detail = host_verdict(results)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-20s %s", state, detail)
        for line in repair_lines(state):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    state, detail = gateway_verdict(matrices.get("direct"),
                                    matrices.get("gateway"))
    emit = log.warning if state in FINDINGS else log.info
    emit("%-20s %s", state, detail)
    for line in repair_lines(state):
        emit("  repair: %s", line)
    if state in FINDINGS:
        findings += 1

    for version, state, detail in declared_findings(matrices.get("direct"),
                                                    declared):
        log.warning("%-20s %s: %s", state, version, detail)
        for line in repair_lines(state):
            log.warning("  repair: %s", line)
        findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
