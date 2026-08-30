"""Prove whether a 403 is about where the request left from, or about the key.

Read only. One GET of /v1/models per provider whose key is present, and
nothing else. No request body is constructed, nothing is generated, nothing is
billed, and no third-party service is contacted -- in particular the script
never looks up its own public IP, because the provider's own answer to "is this
location allowed" is the only authority that matters.

The variable here is the machine. Every other reading in this section is the
same wherever it is taken from; this one only exists relative to a location, so
the unit of evidence is a pair. Run it once from a host you trust, carry the
one-line observation it prints, and run it again from the production egress
path with that line in LLM_EGRESS_BASELINE.

The blob carries a provider, a status and an error code. It never contains a
key, a hostname or anything else, and there is a test that says so.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("llm_egress_region_probe")

# Both are model listings: free, read-only, and refused by a geographic block
# exactly as any other call from the same host would be.
PROVIDERS = {
    "openai": {"url": "https://api.openai.com/v1/models",
               "env": "OPENAI_API_KEY"},
    "anthropic": {"url": "https://api.anthropic.com/v1/models",
                  "env": "ANTHROPIC_API_KEY"},
}

# The one code this script treats as proof of a geographic block, because it is
# the one that is documented. Anthropic publishes a supported-regions list and
# no distinct code for this case, so an Anthropic 403 is recorded verbatim and
# graded as unexplained rather than assigned a cause with no source.
BLOCK_CODE = "unsupported_country_region_territory"

FINDINGS = ("geography-isolated", "region-blocked-unconfirmed",
            "region-blocked-everywhere", "forbidden-unexplained")


def error_code(body):
    """The provider's error code from a JSON body. Pure. Empty when absent.

    One function for both envelopes: OpenAI puts a machine-readable string in
    error.code and Anthropic puts one in error.type, and falling back covers
    both without branching on the provider.
    """
    error = (body or {}).get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    for field in ("code", "type"):
        value = error.get(field)
        if value:
            return str(value).strip()
    return ""


def observation(provider, status, body):
    """One probe result, reduced. Pure. The only thing that leaves the process.

    Three fields on purpose. A response body can contain an organization name,
    a request id or a message quoting the request, and none of that needs to be
    carried between two machines to answer a question about a status code.
    """
    return {"provider": str(provider),
            "status": None if status is None else int(status),
            "code": str(error_code(body) or "")}


def classify(obs):
    """Grade one observation. Pure. Returns (state, detail)."""
    obs = obs or {}
    status = obs.get("status")
    code = str(obs.get("code") or "")
    if status is None:
        return ("unreachable",
                "no response at all, which is a network answer rather than a "
                "policy one")
    status = int(status)
    if status == 200:
        return ("reachable", "this egress path is allowed for this key")
    if status == 403 and code == BLOCK_CODE:
        return ("region-blocked", BLOCK_CODE)
    if status == 403:
        return ("forbidden-other",
                "403 with code %r, which is not the documented geographic block"
                % (code or "(none returned)"))
    if status == 401:
        return ("credentials", "401, which is the key and not the location")
    if status == 429:
        return ("rate-limited",
                "429, so this host reaches the provider fine and the question "
                "is capacity rather than geography")
    return ("unexpected", "%d with code %r" % (status, code or "(none)"))


def compare(local, baseline):
    """The note. Pure. Returns (state, detail). The only two-host function.

    A 403 read alone sends people to the key page. A 403 here beside a 200
    there, on the same key, makes the credential impossible as an explanation,
    and that is the entire reason this script asks to be run twice.
    """
    local_state, local_detail = classify(local)
    provider = (local or {}).get("provider") or "(unknown)"
    if not baseline:
        if local_state == "region-blocked":
            return ("region-blocked-unconfirmed",
                    "%s: 403 %s from this host. The code is documented, but "
                    "with no baseline this has not been separated from an "
                    "account-level restriction" % (provider, BLOCK_CODE))
        if local_state in ("forbidden-other", "credentials"):
            return ("no-baseline",
                    "%s: %s, and no baseline to compare it against. Run this "
                    "from a host you trust first" % (provider, local_detail))
        return (("clear" if local_state == "reachable" else local_state),
                "%s: %s" % (provider, local_detail))

    base_state, _ = classify(baseline)
    if local_state == "reachable":
        return ("clear",
                "%s: 200 from this host, so the egress path is fine"
                % provider)
    if local_state == "region-blocked" and base_state == "reachable":
        return ("geography-isolated",
                "%s: 403 here and 200 from the baseline host on the same key, "
                "so the difference is the egress path and not the credential"
                % provider)
    if local_state == "region-blocked" and base_state == "region-blocked":
        return ("region-blocked-everywhere",
                "%s: 403 %s from both hosts, so this is the account or an "
                "organization-level restriction rather than this deployment's "
                "location" % (provider, BLOCK_CODE))
    if local_state == "credentials" and base_state == "credentials":
        return ("credentials-not-geography",
                "%s: 401 from both hosts on the same key, which is the "
                "credential and not the location" % provider)
    if local_state == "credentials":
        return ("credentials-here-only",
                "%s: 401 here and %s from the baseline host. A key that "
                "authenticates elsewhere and not here is usually a different "
                "key in the environment, not a geographic block"
                % (provider, base_state))
    if local_state == "forbidden-other" and base_state == "reachable":
        return ("forbidden-unexplained",
                "%s: %s here and 200 from the baseline host. The host is the "
                "difference; the code is not one this script can attribute"
                % (provider, local_detail))
    return ("inconclusive",
            "%s: %s here, %s from the baseline host"
            % (provider, local_state, base_state))


def blob(observations):
    """The one line to carry to the other host. Pure. Sorted, and no secrets.

    Keys are sorted so two runs of the same script produce a byte-identical
    string, which makes it obvious when the thing being pasted around has
    changed.
    """
    payload = {}
    for obs in observations or []:
        row = obs or {}
        payload[str(row.get("provider"))] = {
            "status": row.get("status"),
            "code": str(row.get("code") or ""),
        }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def load_baseline(raw):
    """{provider: observation} from the blob. Pure. Empty dict on anything odd.

    Deliberately forgiving. The blob is pasted between machines by a human
    under time pressure, and a mangled paste should produce "no baseline" and a
    clear instruction rather than a stack trace on the host that is on fire.
    """
    try:
        parsed = json.loads(str(raw or "").strip() or "{}")
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for provider, row in parsed.items():
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        try:
            status = None if status is None else int(status)
        except (TypeError, ValueError):
            status = None
        out[str(provider)] = {"provider": str(provider), "status": status,
                              "code": str(row.get("code") or "")}
    return out


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed.

    A region pin, never a proxy. Routing the provider's host through an egress
    somewhere else defeats a restriction rather than resolving one, and this
    section prints repairs that are meant to be run.
    """
    pin = ("pin execution to a supported region. On Vercel, export const "
           "config = { regions: ['iad1'] }. On Cloud Run, Lambda or a "
           "container, redeploy in a supported region. On a VPN, turn it off.")
    no_proxy = ("do not route the provider host through another egress to get "
                "around this. Move the workload, not the packets.")
    if state == "geography-isolated":
        return [pin, no_proxy]
    if state == "region-blocked-unconfirmed":
        return ["run this same script from a host you already trust and paste "
                "its blob into LLM_EGRESS_BASELINE here. One 403 does not "
                "separate the location from the account.", pin]
    if state == "region-blocked-everywhere":
        return ["both hosts are refused, so moving this deployment will not "
                "help. Check the organization's country and any access "
                "restriction on the account before touching infrastructure."]
    if state == "credentials-not-geography":
        return ["not this note. The same key is refused from both hosts, which "
                "is a credential question: check that the key exists, is "
                "enabled, and belongs to the project you think it does."]
    if state == "credentials-here-only":
        return ["compare the environment on the two hosts. A key that works "
                "from one machine and 401s from another is almost always a "
                "different value in the environment rather than a location."]
    if state == "forbidden-unexplained":
        return ["record the error code exactly as printed and check the "
                "provider's supported regions list for the country this host "
                "egresses from.", pin]
    if state == "no-baseline":
        return ["run this from a host you trust and set LLM_EGRESS_BASELINE to "
                "the blob it prints. Without the pair there is one status code "
                "and no conclusion."]
    return []


def probe(provider, key, timeout=30):
    """One GET. Returns (status, body). Never raises on a 4xx: it is the answer."""
    spec = PROVIDERS[provider]
    headers = {}
    if provider == "openai":
        headers["Authorization"] = "Bearer " + key
    else:
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    try:
        r = requests.get(spec["url"], headers=headers, params={"limit": 1},
                         timeout=timeout)
    except requests.RequestException as exc:
        log.debug("probe of %s failed: %s", provider, exc)
        return (None, None)
    try:
        return (r.status_code, r.json())
    except ValueError:
        return (r.status_code, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", default=os.environ.get("LLM_EGRESS_BASELINE"),
                    help="the blob printed by a run on a host you trust")
    args = ap.parse_args()

    present = [p for p in sorted(PROVIDERS)
               if os.environ.get(PROVIDERS[p]["env"])]
    if not present:
        log.error("set OPENAI_API_KEY or ANTHROPIC_API_KEY. Both are used for "
                  "one read-only GET of /v1/models and nothing else")
        return 2

    baseline = load_baseline(args.baseline)
    observations = []
    findings = 0

    for provider in present:
        status, body = probe(provider, os.environ[PROVIDERS[provider]["env"]])
        obs = observation(provider, status, body)
        observations.append(obs)
        state, detail = classify(obs)
        emit = log.warning if state != "reachable" else log.info
        emit("%-11s %s  %-14s %s", provider,
             "---" if obs["status"] is None else obs["status"], state, detail)

        verdict, why = compare(obs, baseline.get(provider))
        emit = log.warning if verdict in FINDINGS else log.info
        emit("%-20s %s", verdict, why)
        for line in repair_lines(verdict):
            emit("  repair: %s", line)
        if verdict in FINDINGS:
            findings += 1

    log.info("baseline: %s", blob(observations))
    if not baseline:
        log.info("no baseline was supplied. Carry that line to the other host "
                 "and run this again there")
    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
