"""Compare the context window a Claude model reports with the one your code enforces.

Read only. One GET per configured model id against the Models API with a
workspace key. No message is ever sent and no long request is constructed.

The API cannot read your source tree, so the enforced ceiling, the beta headers
still in the request path and any surviving long-context price branch are
declared in a small JSON file. That declaration is half the comparison and the
script says so rather than pretending to have discovered it.

There is deliberately no beta-header probe. GET /v1/models with
anthropic-beta: context-1m-2025-08-07 returns 200 whether or not the beta does
anything on any model, because the name is still recognised. Acceptance is not
effect, and a script that read that 200 as confirmation would report health on
exactly the configuration that is broken.

Every repair is printed. Raising a context ceiling is a deploy.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_context_window_cap")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

BETA_1M = "context-1m-2025-08-07"
BETA_RETIRED_ON = "2026-04-30"
STANDARD_WINDOW = 200_000
LONG_WINDOW = 1_000_000

FINDINGS = ("capped-in-code", "ceiling-below-model", "cap-above-model",
            "inert-beta-header", "retired-beta", "phantom-premium")


def _int(value):
    """Read a positive integer, or None. Pure. Absent is never zero.

    None and 0 mean very different things about a context window, and a reader
    that collapses them reports a model with no window rather than a model that
    did not say.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def valid_model_id(model_id):
    """Is this a plausible model id? Pure.

    The guard that stops a line of a config file becoming a URL path segment.
    Model ids are letters, digits, hyphens, dots and underscores, and anything
    else is discarded rather than sent.
    """
    text = str(model_id or "").strip()
    if not text or len(text) > 128:
        return False
    if not text[0].isalpha():
        return False
    return all(ch.isalnum() or ch in "-._" for ch in text)


def parse_rules(config):
    """Read the declared per-model rules. Pure. Invalid ids are dropped.

    Each rule carries the enforced input ceiling, any anthropic-beta values the
    request path still sends, and whether a long-context price or throttle
    branch still exists.
    """
    rules = {}
    for model_id, raw in (config or {}).items():
        if not valid_model_id(model_id):
            continue
        row = raw if isinstance(raw, dict) else {}
        betas = row.get("beta_headers")
        if isinstance(betas, str):
            betas = [betas]
        rules[str(model_id).strip()] = {
            "cap": _int(row.get("max_input_tokens")),
            "betas": [str(b).strip().lower() for b in (betas or [])
                      if str(b or "").strip()],
            "premium": bool(row.get("long_context_premium")),
        }
    return rules


def reported_window(model):
    """max_input_tokens off the model object. Pure. None when absent."""
    return _int((model or {}).get("max_input_tokens"))


def reported_output(model):
    """max_output_tokens off the model object. Pure. Context, never graded here."""
    return _int((model or {}).get("max_output_tokens"))


def shortfall(reported, enforced):
    """Tokens of window that exist and cannot be reached. Pure. None when unknown."""
    if reported is None or enforced is None:
        return None
    return max(0, reported - enforced)


def grade_ceiling(reported, enforced):
    """The enforced ceiling against the reported window. Pure. (state, detail) or None."""
    if reported is None:
        return ("window-not-reported",
                "the model object carried no max_input_tokens, so no claim is "
                "made about the enforced ceiling")
    if enforced is None:
        return None
    if enforced > reported:
        return ("cap-above-model",
                "model reports %d, code enforces %d: the first request over "
                "the reported window returns 400 prompt is too long"
                % (reported, enforced))
    gap = shortfall(reported, enforced)
    if gap == 0:
        return ("aligned", "model reports %d, code enforces %d"
                % (reported, enforced))
    if reported >= LONG_WINDOW and enforced <= STANDARD_WINDOW:
        return ("capped-in-code",
                "model reports %d, code enforces %d: %d token(s) of window "
                "bought and unreachable" % (reported, enforced, gap))
    return ("ceiling-below-model",
            "model reports %d, code enforces %d: %d token(s) of window left "
            "unused" % (reported, enforced, gap))


def grade_betas(reported, betas):
    """Every declared beta header against the window the model reports. Pure.

    The same header name is two different findings depending on the model. On a
    model that already defaults to 1M it is inert. On one that reports the
    standard window it is a retired beta, and the code around it is relying on
    something that stopped working.
    """
    out = []
    for beta in betas or []:
        if beta != BETA_1M:
            continue
        if reported is None:
            continue
        if reported >= LONG_WINDOW:
            out.append(("inert-beta-header",
                        "%s is sent here and does nothing: the 1M window is "
                        "the default on this model and needs no header"
                        % BETA_1M))
        else:
            out.append(("retired-beta",
                        "model reports %d and %s was retired for the Sonnet "
                        "4.5 and Sonnet 4 family on %s: over the standard "
                        "window this id now returns 400, header or not"
                        % (reported, BETA_1M, BETA_RETIRED_ON)))
    return out


def grade_premium(reported, premium):
    """A surviving long-context price or throttle branch. Pure. None when absent."""
    if not premium:
        return None
    if reported is None or reported < LONG_WINDOW:
        return None
    return ("phantom-premium",
            "a long-context price or throttle branch is declared for this "
            "model, and there is no long-context premium: a 900k-token request "
            "bills at the same per-token rate as a 9k one, and the dedicated "
            "1M rate limits were removed")


def audit(model, rule):
    """Every finding for one model. Pure. Returns a list of (state, detail).

    A list rather than a single state on purpose. One stale id routinely
    carries three at once - a ceiling frozen at 200k, an inert header, and a
    premium branch pricing something that is free - and collapsing them to the
    first would hide two repairs behind one line.
    """
    rule = rule or {}
    reported = reported_window(model)
    out = []
    ceiling = grade_ceiling(reported, rule.get("cap"))
    if ceiling is not None:
        out.append(ceiling)
    out.extend(grade_betas(reported, rule.get("betas")))
    premium = grade_premium(reported, rule.get("premium"))
    if premium is not None:
        out.append(premium)
    return out


def repair_lines(state, model_id):
    """The repair for one finding. Pure. Printed, never performed."""
    if state == "capped-in-code":
        return [
            "raise the enforced ceiling for %s to the window the model "
            "reports, then delete the truncation path that exists to serve "
            "the old one." % model_id,
            "read the ceiling from the model object at start-up instead of "
            "hardcoding it, and this cannot drift again when the id rotates.",
        ]
    if state == "ceiling-below-model":
        return ["the enforced ceiling for %s is below the reported window. "
                "Confirm that is deliberate rather than inherited." % model_id]
    if state == "cap-above-model":
        return ["this direction fails loudly rather than quietly: count a real "
                "payload against the reported window before you send it."]
    if state == "inert-beta-header":
        return ["delete %s from the request path for %s. It is not harmful and "
                "it is not doing anything, and leaving it in is what keeps the "
                "rest of the obsolete branch alive." % (BETA_1M, model_id)]
    if state == "retired-beta":
        return ["over the standard window this id now returns 400 whatever the "
                "header says. The path forward is a 4.6 or later id, where 1M "
                "is the default and no header is involved."]
    if state == "phantom-premium":
        return ["delete the premium branch and the separate long-context "
                "throttle. Standard account rate limits apply at every context "
                "length now."]
    return []


def get(session, path):
    r = session.get(API + path, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: ANTHROPIC_API_KEY needs to be a "
                         "workspace key that can read the Models API"
                         % r.status_code)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, metavar="FILE",
                    help="JSON: model id to the ceiling your code enforces, "
                         "the anthropic-beta values it sends, and whether a "
                         "long-context price branch still exists")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a workspace key that can read the "
                  "Models API")
        return 2

    try:
        with open(args.config, "r", encoding="utf-8") as fh:
            rules = parse_rules(json.load(fh))
    except (OSError, ValueError) as exc:
        log.error("could not read %s: %s", args.config, exc)
        return 2
    if not rules:
        log.error("no valid model ids in %s", args.config)
        return 2

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION})
    log.info("no beta-header probe is made: %s is still a recognised name, so "
             "a 200 would prove the name is valid and nothing about its effect",
             BETA_1M)

    bad = 0
    for model_id in sorted(rules):
        model = get(session, "/models/" + model_id)
        if model is None:
            log.warning("%-20s %-26s the id no longer resolves on the Models "
                        "API, which is a retirement rather than a ceiling "
                        "problem", "unknown-model-id", model_id)
            bad += 1
            continue

        for state, detail in audit(model, rules[model_id]):
            line = "%-20s %-26s %s" % (state, model_id, detail)
            if state in FINDINGS:
                bad += 1
                log.warning(line)
                for repair in repair_lines(state, model_id):
                    log.warning("  repair: %s", repair)
            else:
                log.info(line)

        out = reported_output(model)
        if out is not None:
            log.info("%-20s %-26s reports max_output_tokens %d, which is a "
                     "separate ceiling and is not graded here",
                     "output-ceiling", model_id, out)

    log.info("%d model(s) checked, %d finding(s)", len(rules), bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
