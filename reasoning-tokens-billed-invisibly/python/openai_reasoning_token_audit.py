"""Find the cost jump that is reasoning tokens rather than traffic or prompts.

Read only. Two GET requests and nothing else: OPENAI_ADMIN_KEY must be an
organization admin key (sk-admin-...) with read scopes, because /v1/organization
endpoints reject project keys. The repair is printed, never performed, because
this script holds a credential that can spend money on inference.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_reasoning_token_audit")

API = "https://api.openai.com/v1"


def totals(buckets):
    """Sum a list of usage buckets into one row. Pure.

    OpenAI's completions usage carries num_model_requests; Anthropic's messages
    usage report does not carry any request count at all, so a caller working
    against that side gets requests == 0 here and the verdict falls back to a
    weaker ratio rather than dividing by nothing.
    """
    row = {"requests": 0, "input": 0, "output": 0, "buckets": 0}
    for b in buckets:
        row["buckets"] += 1
        for r in b.get("results", []) or []:
            row["requests"] += int(r.get("num_model_requests") or 0)
            row["input"] += int(r.get("input_tokens")
                                or r.get("uncached_input_tokens") or 0)
            row["output"] += int(r.get("output_tokens") or 0)
    return row


def split(buckets, now, window_days=7):
    """Cut a daily series into (prior, recent) around a boundary. Pure, clock
    passed in, so the boundary in a test is a date you can read rather than a
    function of when the suite happened to run.

    Buckets older than twice the window are dropped: comparing last week against
    a quarter ago answers a different question than the one being asked.
    """
    edge = now.timestamp() - window_days * 86400
    floor = now.timestamp() - 2 * window_days * 86400
    prior, recent = [], []
    for b in buckets:
        start = b.get("start_time")
        if not isinstance(start, (int, float)):
            continue
        if start >= edge:
            recent.append(b)
        elif start >= floor:
            prior.append(b)
    return prior, recent


def verdict(prior, recent, jump=1.5, flat=0.2):
    """Say which of the four explanations for a cost jump the numbers support.

    Pure. `jump` is the factor that counts as a step change; `flat` is how far
    the other ratio may move and still be called unchanged.

    Returns (state, detail).
    """
    a, b = totals(prior), totals(recent)

    if not b["requests"] and not b["output"]:
        return ("no-data", "no usage in the recent window")

    if b["requests"] and not b["output"]:
        return ("failing-before-generation",
                "%d request(s) in the recent window generated zero output "
                "tokens. Those calls were rejected before the model ran; that "
                "is an error shape and not a reasoning one." % b["requests"])

    if not a["requests"] or not b["requests"]:
        # Anthropic's usage report has no request count, so this is the honest
        # fallback rather than a per-request claim that cannot be made.
        if a["input"] and b["input"]:
            before = a["output"] / a["input"]
            after = b["output"] / b["input"]
            if before and after / before >= jump:
                return ("unmeasurable-but-rising",
                        "no request count in these buckets, so this is output "
                        "per input token, not per request: %.2f to %.2f. "
                        "Consistent with reasoning, but prompt shrinkage looks "
                        "identical." % (before, after))
            return ("unmeasurable",
                    "no request count in these buckets. Output per input token "
                    "is %.2f against %.2f before, which is the strongest claim "
                    "available without a request count." % (after, before))
        return ("unmeasurable",
                "no request count and no input tokens to fall back on")

    in_before = a["input"] / a["requests"]
    in_after = b["input"] / b["requests"]
    out_before = a["output"] / a["requests"]
    out_after = b["output"] / b["requests"]
    numbers = ("%.0f to %.0f output tokens per request, %.0f to %.0f input"
               % (out_before, out_after, in_before, in_after))

    out_factor = (out_after / out_before) if out_before else 0.0
    in_factor = (in_after / in_before) if in_before else 0.0

    if out_factor >= jump and abs(in_factor - 1.0) <= flat:
        return ("reasoning-tax",
                "%s. Output per request rose %.1fx while input per request held "
                "steady. Those tokens were generated and billed at the output "
                "rate and never returned to you." % (numbers, out_factor))

    if out_factor >= jump and in_factor >= jump:
        return ("longer-prompts",
                "%s. Both ratios rose together, so the prompts grew. Raising "
                "reasoning effort does not move the input side."
                % numbers)

    if b["requests"] >= a["requests"] * jump:
        return ("volume-only",
                "%s. Requests rose from %d to %d with the ratios unchanged: the "
                "bill grew because traffic grew, and unit economics did not "
                "move." % (numbers, a["requests"], b["requests"]))

    return ("steady", numbers)


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization endpoints need an "
                         "organization admin key, not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def usage_by_model(session, since, days):
    """Read daily completion usage grouped by model, following next_page."""
    params = [("start_time", int(since.timestamp())), ("bucket_width", "1d"),
              ("limit", max(days, 1)), ("group_by[]", "model")]
    out = {}
    while True:
        page = get(session, "/organization/usage/completions", params)
        for b in page.get("data", []):
            for r in b.get("results", []) or []:
                model = r.get("model") or "unspecified"
                out.setdefault(model, []).append(
                    {"start_time": b.get("start_time"), "results": [r]})
        if not page.get("has_more") or not page.get("next_page"):
            break
        params = [p for p in params if p[0] != "page"] + [("page", page["next_page"])]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="how far back to read daily usage buckets")
    ap.add_argument("--window", type=int, default=7,
                    help="days in the recent window, compared against the days before it")
    ap.add_argument("--jump", type=float, default=1.5,
                    help="factor that counts as a step change")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key with read "
                  "scopes; project keys are rejected by /v1/organization/*)")
        return 2

    now = dt.datetime.now(dt.timezone.utc)
    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})

    since = now - dt.timedelta(days=args.days)
    by_model = usage_by_model(s, since, args.days)
    if not by_model:
        log.info("no completion usage in the last %d day(s)", args.days)
        return 0

    bad = 0
    for model, buckets in sorted(by_model.items()):
        prior, recent = split(buckets, now, args.window)
        state, detail = verdict(prior, recent, args.jump)
        line = "%-22s %-26s %s" % (model, state, detail)
        if state in ("steady", "volume-only", "no-data", "unmeasurable"):
            log.info(line)
            continue
        bad += 1
        log.warning(line)
        if state in ("reasoning-tax", "unmeasurable-but-rising"):
            log.warning("  repair: lower the reasoning effort on this model for "
                        "tasks that do not need deliberation, and drop the "
                        "higher modes unless an eval justifies them. Log "
                        "usage.output_tokens_details.reasoning_tokens per call "
                        "so the invisible half shows up in your own metrics.")
            log.warning("  cross-check the money: GET %s/organization/costs"
                        "?start_time=%d&bucket_width=1d&group_by[]=line_item",
                        API, int(since.timestamp()))

    log.info("%d model(s) over %d day(s), %d finding(s)",
             len(by_model), args.days, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
