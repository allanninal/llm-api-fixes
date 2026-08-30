"""Report OpenAI request volume growing faster than the tokens it carries.

Read only. One GET against the organization usage report, plus one per finding
against the project rate limits. Both need an organization admin key
(sk-admin-), which can be provisioned read-only.

Everything here comes from the provider's own numbers: num_model_requests and
the token counts arrive on the same result object, so no telemetry of your own
is required. Requests climbing while tokens stay flat, with the mean call size
collapsing underneath, is the retry-storm signature and nothing else makes it.

The repair is printed, never performed. Retry layering lives in your client.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_retry_storm_shape")

API = "https://api.openai.com/v1"

FINDINGS = ("retry-storm", "requests-outpacing-tokens")


def _int(value):
    """Read a usage field as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def series(buckets):
    """Per (project, model), the hourly points. Pure.

    Buckets are kept rather than totalled, because the whole method is a
    comparison between two halves of the window and a sum has already thrown
    the halves away.
    """
    out = {}
    for bucket in buckets or []:
        start = _int(bucket.get("start_time"))
        for result in bucket.get("results") or []:
            key = (str(result.get("project_id") or "unknown"),
                   str(result.get("model") or "unknown"))
            out.setdefault(key, []).append({
                "start": start,
                "requests": _int(result.get("num_model_requests")),
                "tokens": (_int(result.get("input_tokens"))
                           + _int(result.get("output_tokens"))),
            })
    for points in out.values():
        points.sort(key=lambda p: p["start"])
    return out


def fold_windows(points, cutoff, partial_after=None):
    """Sum one series into (prior, recent) either side of a cutoff. Pure.

    Points at or after partial_after are dropped. The hour the clock is still
    inside is always short, and a growth ratio computed with it in reports a
    decline every single time the job runs before the hour is up.
    """
    prior = {"requests": 0, "tokens": 0, "buckets": 0}
    recent = {"requests": 0, "tokens": 0, "buckets": 0}
    for point in points or []:
        start = _int(point.get("start"))
        if partial_after is not None and start >= partial_after:
            continue
        window = recent if start >= cutoff else prior
        window["requests"] += _int(point.get("requests"))
        window["tokens"] += _int(point.get("tokens"))
        window["buckets"] += 1
    return prior, recent


def growth(prior_value, recent_value):
    """recent / prior, or None when there is nothing to divide by. Pure.

    None rather than infinity. A workload that did not exist last week has no
    growth rate, and reporting one as an enormous number puts every new
    deployment at the top of the report.
    """
    prior_value = float(prior_value or 0)
    if prior_value <= 0:
        return None
    return float(recent_value or 0) / prior_value


def tokens_per_request(window):
    """Mean tokens per request in one window, or None. Pure."""
    made = _int((window or {}).get("requests"))
    if made <= 0:
        return None
    return _int(window.get("tokens")) / float(made)


def divergence_ratio(prior, recent):
    """Request growth divided by token growth. Pure. None when unavailable.

    Worth stating in the code rather than only in the prose: this number is
    exactly the reciprocal of the change in tokens per request. The first
    version of this script tested both and read them as two agreeing witnesses.
    They are one witness stated twice, so the corroboration has to come from
    somewhere else, which is what burstiness() is for.
    """
    request_growth = growth(_int((prior or {}).get("requests")),
                            _int((recent or {}).get("requests")))
    token_growth = growth(_int((prior or {}).get("tokens")),
                          _int((recent or {}).get("tokens")))
    if request_growth is None or token_growth is None or token_growth <= 0:
        return None
    return request_growth / token_growth


def burstiness(points, cutoff, partial_after=None, top_share=0.1, min_buckets=24):
    """Share of the recent window's requests in its busiest hours. Pure.

    The busiest top_share of hours, by request count. Evenly spread traffic
    puts about top_share of its requests there. A retry storm puts most of them
    there, because retries amplify during the failures that caused them, and
    that concentration is the only evidence in this report that is independent
    of the growth ratio.

    None when there are too few hours for the share to mean anything.
    """
    recent = []
    for point in points or []:
        start = _int(point.get("start"))
        if partial_after is not None and start >= partial_after:
            continue
        if start >= cutoff:
            recent.append(_int(point.get("requests")))
    if len(recent) < min_buckets:
        return None
    total = sum(recent)
    if total <= 0:
        return None
    top = max(1, int(round(len(recent) * top_share)))
    return sum(sorted(recent, reverse=True)[:top]) / float(total)


def classify(prior, recent, burst=None, divergence=2.0, min_requests=1000,
             burst_floor=0.35):
    """Compare two windows of one series. Pure. Returns (state, detail).

    Four ways two series can move relative to each other, and only one of them
    is a retry storm. The divergence says the request count grew on its own;
    the burst share says whether it grew in the shape retries have.
    """
    prior = prior or {}
    recent = recent or {}
    prior_requests = _int(prior.get("requests"))
    recent_requests = _int(recent.get("requests"))

    if prior_requests < min_requests and recent_requests < min_requests:
        return ("too-little-traffic",
                "%d request(s) then %d, both under the floor of %d"
                % (prior_requests, recent_requests, min_requests))

    request_growth = growth(prior_requests, recent_requests)
    token_growth = growth(_int(prior.get("tokens")), _int(recent.get("tokens")))
    if request_growth is None or token_growth is None:
        return ("new-workload",
                "nothing in the prior window to compare against: %d request(s) "
                "and %d token(s) appeared this week"
                % (recent_requests, _int(recent.get("tokens"))))

    before = tokens_per_request(prior) or 0.0
    after = tokens_per_request(recent) or 0.0
    shape = ("requests x%.2f, tokens x%.2f, tokens per request %d then %d"
             % (request_growth, token_growth, before, after))
    if burst is not None:
        shape += ("; %.0f%% of the surplus landed in the busiest 10%% of hours"
                  % (burst * 100))

    if request_growth >= divergence * token_growth:
        if burst is None:
            return ("retry-storm",
                    shape + ". Too few hourly buckets to measure how "
                    "concentrated the surplus was, so this rests on the growth "
                    "ratio alone.")
        if burst < burst_floor:
            return ("requests-outpacing-tokens",
                    shape + ". The extra calls are spread evenly across the "
                    "hours rather than piled into a few, which is a workload "
                    "that got shorter rather than one being retried.")
        return ("retry-storm",
                shape + ". The surplus arrived in bursts, which is what "
                "retries do: they amplify during the failures that caused "
                "them.")

    if token_growth >= divergence * request_growth:
        return ("prompts-grew",
                shape + ". Tokens moved and the call count did not, so this is "
                "prompt or answer length, not call volume.")

    if request_growth >= 1.25 and token_growth >= 1.25:
        return ("traffic-growth",
                shape + ". Both series moved together, which is traffic rather "
                "than amplification.")

    if request_growth <= 0.75:
        return ("quieter", shape + ". Fewer calls than the week before.")

    return ("steady", shape + ".")


def rate_limit_values(payload, model):
    """The RPM and TPM this project publishes for a model. Pure.

    Longest matching prefix wins, so a dated id resolves to the most specific
    entry that claims it rather than to whichever one came back first.
    """
    best_key, best_len = None, -1
    name = str(model or "").strip().lower()
    for entry in (payload or {}).get("data") or []:
        candidate = str(entry.get("model") or "").strip().lower()
        if not candidate:
            continue
        if name == candidate or name.startswith(candidate):
            if len(candidate) > best_len:
                best_key, best_len = entry, len(candidate)
    if best_key is None:
        return {"requests": None, "tokens": None}
    out = {}
    for field, key in (("max_requests_per_1_minute", "requests"),
                       ("max_tokens_per_1_minute", "tokens")):
        try:
            out[key] = int(best_key.get(field))
        except (TypeError, ValueError):
            out[key] = None
    return out


def limiter_pressure(window, hours, limits, near=0.7, idle=0.3):
    """Where a window's mean traffic sits against RPM and TPM. Pure.

    Hourly buckets cannot resolve a minute, so this is the hourly mean spread
    across sixty minutes: a floor on the real peak and never the peak itself.
    If even the mean is near the ceiling then the peak went past it long ago,
    which is all this needs to say.
    """
    limits = limits or {}
    rpm_limit = limits.get("requests")
    tpm_limit = limits.get("tokens")
    minutes = max(1, int(hours or 0) * 60)
    if not rpm_limit and not tpm_limit:
        return ("no-limits-published",
                "this project publishes no rate limit for the model, so there "
                "is no ceiling to compare the mean against")

    rpm_used = (_int((window or {}).get("requests")) / float(minutes) / rpm_limit
                if rpm_limit else None)
    tpm_used = (_int((window or {}).get("tokens")) / float(minutes) / tpm_limit
                if tpm_limit else None)
    shape = "hourly mean sits at %s of the RPM ceiling and %s of the TPM ceiling" % (
        "%.0f%%" % (rpm_used * 100) if rpm_used is not None else "an unpublished share",
        "%.0f%%" % (tpm_used * 100) if tpm_used is not None else "an unpublished share")

    if rpm_used is not None and tpm_used is not None:
        if rpm_used >= near and tpm_used <= idle:
            return ("rpm-bound-tpm-idle",
                    shape + ", which is what amplification looks like from the "
                    "limiter side: the request bucket fills and the token "
                    "bucket does not")
        if rpm_used >= near and tpm_used >= near:
            return ("both-near", shape + ", so both limiters are under pressure")
        if tpm_used >= near:
            return ("tpm-bound",
                    shape + ", so the token limiter is the binding one and this "
                    "is volume rather than retries")
    return ("headroom", shape)


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an "
                         "organization admin key (sk-admin-), not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params, max_pages=40):
    """Walk the usage report, which paginates on an opaque page cursor."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, path, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params = dict(params)
        params["page"] = page["next_page"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14,
                    help="days to read, split into two halves (default 14)")
    ap.add_argument("--divergence", type=float, default=2.0,
                    help="how far request growth must outpace token growth "
                         "(default 2.0)")
    ap.add_argument("--min-requests", type=int, default=1000,
                    help="ignore series below this many requests (default 1000)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print series that moved together")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key; read-only "
                  "scopes are enough)")
        return 2

    days = max(2, min(int(args.days), 30))
    half = days // 2
    now = int(time.time())
    start = now - days * 86400
    cutoff = now - half * 86400
    partial_after = now - (now % 3600)

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + admin})

    buckets = pages(session, "/organization/usage/completions", {
        "start_time": start,
        "bucket_width": "1h",
        "limit": 168,
        "group_by": ["model", "project_id"],
    })
    rows = series(buckets)
    if not rows:
        log.info("no completions usage in the last %d day(s)", days)
        return 0

    checked = 0
    bad = 0
    for project, model in sorted(rows):
        points = rows[(project, model)]
        prior, recent = fold_windows(points, cutoff, partial_after)
        burst = burstiness(points, cutoff, partial_after)
        state, detail = classify(prior, recent, burst, args.divergence,
                                 min_requests=args.min_requests)
        checked += 1
        line = "%-26s %s / %s  %s" % (state, project, model, detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            limits = {"requests": None, "tokens": None}
            if project != "unknown":
                try:
                    limits = rate_limit_values(
                        get(session, "/organization/projects/%s/rate_limits" % project,
                            {"limit": 100}), model)
                except (requests.RequestException, SystemExit):
                    limits = {"requests": None, "tokens": None}
            _, pressure = limiter_pressure(recent, half * 24, limits)
            log.warning("  %s", pressure)
            if state == "retry-storm":
                log.warning("  repair: collapse to one retry layer. Set "
                            "max_retries explicitly on the SDK client and "
                            "remove the outer wrapper, or set it to 0 and keep "
                            "the wrapper. Exponential backoff with jitter, and "
                            "a circuit breaker so a sustained failure stops "
                            "re-amplifying.")
                log.warning("  repair: raising the project rate limit is the "
                            "second measure, not the first. An admin can call "
                            "POST /v1/organization/projects/{project_id}"
                            "/rate_limits/{rate_limit_id} once the layering is "
                            "fixed. It is printed here, not run.")
            else:
                log.warning("  repair: nothing yet. Confirm the shorter calls "
                            "are a real workload before changing any retry "
                            "policy, and re-run next week.")
        elif args.show_all:
            log.info(line)

    log.info("%d model/project series checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
