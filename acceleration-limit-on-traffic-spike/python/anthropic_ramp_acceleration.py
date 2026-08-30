"""Find 429s caused by the ramp rather than by the limit.

Read only. Two GETs with an Admin API key:

  GET /v1/organizations/usage_report/messages?bucket_width=1m&group_by[]=model
  GET /v1/organizations/rate_limits

The finding is a shape between two adjacent minutes, not a level in one. A steep
step whose peak never approaches the configured ceiling is the acceleration
signature: a sharp increase in usage can produce 429s on its own, and limits
expressed per minute can be enforced over shorter intervals.

Saturation is graded first. A peak that does reach the ceiling has an ordinary
explanation and belongs to the ITPM or OTPM notes, and this script says so
instead of reporting a ramp next to it.

Input is summed the way the limiter counts it: uncached input plus both cache
creation figures, and not cache reads. This report has no request count of any
kind, so the ramp is measured in tokens and reported as such.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_ramp_acceleration")

API = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# The published Start tier figures, as (ITPM, OTPM), used only to spot an
# organization whose configured numbers sit below the documented floor, which
# is what the Evaluation tier looks like from the outside. This is a
# documentation table and documentation tables move; it is printed as "below
# the published Start tier", never as a claim about what your tier is.
START_TIER = {
    "claude-fable-5": (500_000, 100_000),
    "claude-haiku-3-5": (100_000, 20_000),
}
START_TIER_DEFAULT = (2_000_000, 400_000)

# Claude Haiku 3.5 counts cache reads toward ITPM. Every other current model
# does not. Applying one rule to both is how a caching workload gets reported
# as saturated when it is nowhere near its limit.
COUNTS_CACHE_READS = ("claude-haiku-3-5",)

SATURATED = 0.85
QUIET = 0.60

FINDINGS = ("acceleration-suspect", "ramp-near-ceiling", "below-published-start")


def num(value):
    """A float, or 0.0. Pure."""
    try:
        if value is None or isinstance(value, bool):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def cache_creation(result):
    """Both cache creation figures, summed. Pure.

    They are separate fields for the 5 minute and 1 hour entries and both count
    toward the input limiter, so a reader that knows about only one of them
    undercounts every cached workload.
    """
    block = (result or {}).get("cache_creation") or {}
    return (num(block.get("ephemeral_5m_input_tokens"))
            + num(block.get("ephemeral_1h_input_tokens")))


def uncached_input(result):
    """The input tokens that count toward ITPM. Pure.

    uncached_input_tokens plus cache creation. cache_read_input_tokens is
    deliberately absent: it does not count toward the input limiter on current
    models, and including it inflates a cached workload's every bucket.
    """
    return num((result or {}).get("uncached_input_tokens")) + cache_creation(result)


def series(pages, model_key="model"):
    """{model: [(starting_at, input_tokens, output_tokens, cache_read)]}. Pure.

    Ordered by bucket start. A bucket with an empty results list contributes
    nothing, which keeps a gap in traffic visible as a gap rather than being
    silently closed up by the next minute that had data.
    """
    out = {}
    for page in pages or []:
        for bucket in ((page or {}).get("data") or []):
            start = str((bucket or {}).get("starting_at") or "")
            for result in ((bucket or {}).get("results") or []):
                model = str((result or {}).get(model_key) or "(ungrouped)")
                out.setdefault(model, []).append(
                    (start, uncached_input(result),
                     num((result or {}).get("output_tokens")),
                     num((result or {}).get("cache_read_input_tokens"))))
    for rows in out.values():
        rows.sort(key=lambda r: r[0])
    return out


def peak(rows, index):
    """(starting_at, value) for the largest bucket. Pure. ("", 0.0) if empty."""
    best = ("", 0.0)
    for row in rows or []:
        if row[index] > best[1]:
            best = (row[0], row[index])
    return best


def share(value, limit):
    """value / limit, or None when the limit is unknown. Pure."""
    if not limit or limit <= 0:
        return None
    return float(value) / float(limit)


def ramp_factors(rows, index, min_base=10_000.0):
    """[(prev_start, start, factor, prev, current)] between adjacent minutes.

    Pure, largest factor first. Ratios are computed only where the earlier
    minute is above min_base, because 12 tokens followed by 900 is a 75x ratio
    and means nothing at all. A rise from a genuine standing start is real but
    it is not what this measures, so it is left out rather than dominating.
    """
    out = []
    rows = list(rows or [])
    for i in range(1, len(rows)):
        prev = rows[i - 1][index]
        current = rows[i][index]
        if prev < min_base or current <= prev:
            continue
        out.append((rows[i - 1][0], rows[i][0], current / prev, prev, current))
    out.sort(key=lambda r: (-r[2], r[1]))
    return out


def group_for_model(groups, model):
    """{limiter_type: value} for the group that contains this model. Pure.

    Membership is exact: every model id and alias that counts against a group is
    listed on it, so a prefix match would only ever be a way to get the wrong
    group for a model the API already told you about.
    """
    for entry in groups or []:
        models = [str(m) for m in ((entry or {}).get("models") or [])]
        if str(model) in models:
            out = {}
            for row in ((entry or {}).get("limits") or []):
                ltype = str((row or {}).get("type") or "")
                if ltype:
                    out[ltype] = num((row or {}).get("value"))
            return out
    return {}


def below_published_start(model, limits):
    """[(limiter, configured, published_start)] below the documented floor. Pure."""
    itpm_floor, otpm_floor = START_TIER.get(str(model), START_TIER_DEFAULT)
    out = []
    pairs = (("input_tokens_per_minute", itpm_floor),
             ("output_tokens_per_minute", otpm_floor))
    for ltype, floor in pairs:
        configured = (limits or {}).get(ltype)
        if configured and 0 < configured < floor:
            out.append((ltype, configured, floor))
    return out


def verdict(rows, limits, model, ramp_threshold=3.0):
    """Classify one model's window. Pure. Returns (state, detail, facts).

    Saturation is answered first and handed to the note that owns it. A ramp
    reported next to a saturated limiter would be a coincidence dressed up as a
    cause.
    """
    rows = list(rows or [])
    limits = limits or {}
    facts = {
        "peak_in": peak(rows, 1),
        "peak_out": peak(rows, 2),
        "itpm": limits.get("input_tokens_per_minute"),
        "otpm": limits.get("output_tokens_per_minute"),
        "ramps": ramp_factors(rows, 1) + ramp_factors(rows, 2),
        "cache_read_counts": str(model) in COUNTS_CACHE_READS,
    }
    facts["ramps"].sort(key=lambda r: -r[2])
    in_share = share(facts["peak_in"][1], facts["itpm"])
    out_share = share(facts["peak_out"][1], facts["otpm"])
    facts["in_share"] = in_share
    facts["out_share"] = out_share

    if not rows or (facts["peak_in"][1] <= 0 and facts["peak_out"][1] <= 0):
        return ("no-traffic", "no usage in this window", facts)
    if in_share is not None and in_share >= SATURATED:
        return ("limiter-saturated",
                "input peaked at %s/min, %.0f%% of ITPM. That is the input "
                "limiter note, not this one."
                % (fmt(facts["peak_in"][1]), in_share * 100), facts)
    if out_share is not None and out_share >= SATURATED:
        return ("limiter-saturated",
                "output peaked at %s/min, %.0f%% of OTPM. That is the output "
                "limiter note, not this one."
                % (fmt(facts["peak_out"][1]), out_share * 100), facts)

    steepest = facts["ramps"][0][2] if facts["ramps"] else 0.0
    if steepest < ramp_threshold:
        return ("steady",
                "no adjacent minute rose by %.1fx or more" % ramp_threshold, facts)
    quiet = [s for s in (in_share, out_share) if s is not None]
    if quiet and max(quiet) <= QUIET:
        return ("acceleration-suspect",
                "a %.1fx step between adjacent minutes with every peak under "
                "%.0f%% of its ceiling" % (steepest, QUIET * 100), facts)
    return ("ramp-near-ceiling",
            "a %.1fx step between adjacent minutes, and the peak is already "
            "past %.0f%% of a ceiling. Pace it and ask for the increase."
            % (steepest, QUIET * 100), facts)


def fmt(value):
    """Thousands separators. Pure."""
    return "{:,}".format(int(round(num(value))))


def repair_lines(state, facts=None):
    """The repair for one verdict. Pure. Printed, never performed."""
    facts = facts or {}
    if state == "acceleration-suspect":
        lines = ["ramp gradually and keep usage patterns consistent. A step this "
                 "steep can 429 on acceleration alone, well under the tier limits, "
                 "and a limit increase does not change it.",
                 "spread the burst across the minute with client-side pacing or a "
                 "queue in front of the fan-out. A limit of 60 per minute may be "
                 "enforced as 1 per second, so the shape inside the minute matters."]
        if facts.get("cache_read_counts"):
            lines.append("this model counts cache reads toward the input limiter, "
                         "unlike the others. Add cache_read_input_tokens back "
                         "before comparing its peak against ITPM.")
        return lines
    if state == "ramp-near-ceiling":
        return ["pace the ramp and request the increase. Both are true here: the "
                "step is steep enough to trip acceleration and the peak is close "
                "enough that a bigger ceiling would also help."]
    if state == "limiter-saturated":
        return ["this one really is the headline number. Read the input or output "
                "limiter note for the reading that fits, rather than pacing "
                "traffic that is genuinely at its ceiling."]
    if state == "below-published-start":
        return ["configured limits below the published Start tier usually mean an "
                "Evaluation tier organization, where the documentation tables do "
                "not apply. Stop reasoning from the tables and read "
                "/v1/organizations/rate_limits instead.",
                "Evaluation limits rise automatically as the organization builds "
                "usage history, so this is a reason to pace traffic rather than a "
                "reason to file anything."]
    return []


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: the usage report and the rate limits "
                         "endpoint need an Admin API credential, not a workspace "
                         "key" % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, **params):
    params = dict(params)
    for _ in range(50):
        page = get(session, path, **params)
        yield page
        nxt = page.get("next_page")
        if not nxt:
            return
        params["page"] = nxt


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=4.0,
                    help="window to read, in hours (max 24 at minute buckets)")
    ap.add_argument("--ramp", type=float, default=3.0,
                    help="adjacent-minute factor that counts as a steep step")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not key:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key or another "
                  "organization scoped read credential")
        return 2
    hours = max(0.1, min(24.0, args.hours))

    s = requests.Session()
    s.headers.update({"x-api-key": key,
                      "anthropic-version": ANTHROPIC_VERSION,
                      "User-Agent": "anthropic-ramp-acceleration/1.0"})

    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    start = now - dt.timedelta(hours=hours)
    stamp = "%Y-%m-%dT%H:%M:%SZ"

    buckets = list(pages(s, "/organizations/usage_report/messages",
                         starting_at=start.strftime(stamp),
                         ending_at=now.strftime(stamp),
                         bucket_width="1m", limit=1440,
                         **{"group_by[]": "model"}))
    groups = []
    for page in pages(s, "/organizations/rate_limits"):
        groups.extend(page.get("data") or [])

    by_model = series(buckets)
    minutes = sum(len(page.get("data") or []) for page in buckets)
    log.info("%d minute bucket(s), %d model(s), %d rate limit group(s)",
             minutes, len(by_model), len(groups))

    findings = 0
    for model in sorted(by_model):
        rows = by_model[model]
        limits = group_for_model(groups, model)
        state, detail, facts = verdict(rows, limits, model, args.ramp)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-21s %s: %s", state, model, detail)

        if state in ("acceleration-suspect", "ramp-near-ceiling", "steady"):
            emit("  peak input   %s/min against ITPM %s (%s)",
                 fmt(facts["peak_in"][1]), fmt(facts["itpm"] or 0),
                 "unknown" if facts["in_share"] is None
                 else "%.0f%%" % (facts["in_share"] * 100))
            emit("  peak output  %s/min against OTPM %s (%s)",
                 fmt(facts["peak_out"][1]), fmt(facts["otpm"] or 0),
                 "unknown" if facts["out_share"] is None
                 else "%.0f%%" % (facts["out_share"] * 100))
            if facts["ramps"]:
                prev_at, at, factor, prev, current = facts["ramps"][0]
                emit("  steepest ramp %.1fx between %s and %s (%s -> %s)",
                     factor, prev_at[11:16] or prev_at, at[11:16] or at,
                     fmt(prev), fmt(current))
            emit("  note: this report carries no request count, so the ramp above "
                 "is measured in tokens. Sub-minute bursting is invisible here.")

        for line in repair_lines(state, facts):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

        for ltype, configured, floor in below_published_start(model, limits):
            log.warning("%-21s %s: configured %s is %s, under the published Start "
                        "tier figure of %s", "below-published-start", model, ltype,
                        fmt(configured), fmt(floor))
            for line in repair_lines("below-published-start"):
                log.warning("  repair: %s", line)
            findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
