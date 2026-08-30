"""Report an Anthropic output limiter that concurrency cannot fix.

Read only. Two GET requests and nothing else against the Admin API, which needs
an Admin API key (sk-ant-admin...); a workspace key is rejected by every
/v1/organizations/* path, and an Admin key can be provisioned read-only.

The repair is printed, never performed. Lowering an effort setting changes what
the model does with a question, and moving traffic to the Batch API changes
when answers arrive. Both are decisions with owners.

The messages usage report has no request-count field. That is why this script
never claims a request rate: it divides the peak output minute by the
configured RPM and prints the answer length at which the request limiter would
have bound first, which is a comparison the reader can make and the API cannot.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_otpm_ceiling")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

LIMITER_TYPES = ("requests_per_minute", "input_tokens_per_minute",
                 "output_tokens_per_minute")

FINDINGS = ("otpm-saturated", "both-limiters-saturated")


def generated(result):
    """Output tokens in one usage result. Pure.

    Thinking tokens are billed as output and counted as output, so they are
    already inside this number. There is no separate field to add and no way to
    subtract them, which is exactly why an effort change can saturate the
    output limiter with nothing else in the request having moved.
    """
    if not isinstance(result, dict):
        return 0
    try:
        return int(result.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def received(result):
    """Input tokens in one usage result, from every field that carries them. Pure.

    Total input is cache_read + cache_creation + uncached. This is only used to
    decide whether the input limiter also had pressure on it, so it is summed
    generously rather than charged the way ITPM charges.
    """
    if not isinstance(result, dict):
        return 0
    total = 0
    for field in ("uncached_input_tokens", "cache_read_input_tokens"):
        try:
            total += int(result.get(field) or 0)
        except (TypeError, ValueError):
            pass
    creation = result.get("cache_creation") or {}
    for field in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
        try:
            total += int(creation.get(field) or 0)
        except (TypeError, ValueError):
            pass
    return total


def peaks(buckets):
    """Fold one-minute buckets into per-model output peaks. Pure.

    The input recorded is the input from the minute output peaked, not the
    largest input minute in the window. The judgement this script makes is
    whether output was full while input had room, and pairing two peaks from
    two different minutes describes a workload that never ran.
    """
    per_minute = {}
    for bucket in buckets or []:
        stamp = str(bucket.get("starting_at") or bucket.get("start_time") or "")
        for result in bucket.get("results") or []:
            model = str(result.get("model") or "").strip() or "all models"
            row = per_minute.setdefault((model, stamp), {"out": 0, "in": 0})
            row["out"] += generated(result)
            row["in"] += received(result)

    out = {}
    for (model, stamp), row in per_minute.items():
        stats = out.setdefault(model, {"peak_out": 0, "peak_at": None,
                                       "input_at_peak": 0, "minutes": 0,
                                       "total_out": 0})
        stats["minutes"] += 1
        stats["total_out"] += row["out"]
        if row["out"] > stats["peak_out"]:
            stats["peak_out"] = row["out"]
            stats["peak_at"] = stamp
            stats["input_at_peak"] = row["in"]
    return out


def limits_by_group(payload):
    """{model_group: {limiter type: value}} from the rate-limits response. Pure.

    All three limiters are kept because all three are needed: output for the
    verdict, input to rule out the sibling finding, and requests to compute the
    answer length at which the request rate would have mattered. A type absent
    from limits[] is None, which means it inherits, never that it is unlimited.
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


def limits_for(groups, model):
    """The limiter row for the group a model id belongs to. Pure. Longest prefix wins."""
    name = str(model or "").strip().lower()
    if not name:
        return None
    best_key, best_len = None, -1
    for group in (groups or {}):
        candidate = str(group).strip().lower()
        if not candidate:
            continue
        if name == candidate or name.startswith(candidate):
            if len(candidate) > best_len:
                best_key, best_len = group, len(candidate)
    if best_key is None:
        return None
    return (groups or {}).get(best_key)


def implied_mean_output(peak_output, rpm):
    """Answer length at which RPM would bind before OTPM. Pure.

    If a minute generated peak_output tokens, the request limiter could only
    have been what stopped you if you were also making rpm calls in that
    minute, which means a mean answer of peak_output / rpm tokens. Longer
    answers than that and the request rate was never close to its ceiling.

    This exists because the usage report has no request count. It converts a
    question the API cannot answer into one the reader already knows.
    """
    if rpm is None or rpm <= 0:
        return None
    try:
        peak = float(peak_output or 0)
    except (TypeError, ValueError):
        return None
    if peak <= 0:
        return None
    return peak / float(rpm)


def output_to_input_ratio(limits):
    """OTPM as a share of ITPM for one model group. Pure.

    Roughly one fifth at every tier, which is the structural reason a
    generation workload reaches the output ceiling first. Printed rather than
    assumed, because a workspace override can change it.
    """
    if not isinstance(limits, dict):
        return None
    otpm = limits.get("output_tokens_per_minute")
    itpm = limits.get("input_tokens_per_minute")
    if otpm is None or itpm is None or itpm <= 0:
        return None
    return otpm / float(itpm)


def verdict(model, stats, limits, floor=0.9, watch=0.6, min_minutes=10):
    """Classify one model's output limiter. Pure. Returns (state, detail)."""
    minutes = int((stats or {}).get("minutes") or 0)
    if minutes < min_minutes:
        return ("too-few-buckets",
                "%d minute(s) of traffic in the window, under the floor of %d. "
                "A peak taken over this little is noise." % (minutes, min_minutes))

    row = limits if isinstance(limits, dict) else {}
    otpm = row.get("output_tokens_per_minute")
    if otpm is None or otpm <= 0:
        return ("no-limit-published",
                "no output_tokens_per_minute is published for this model's "
                "group, so there is no ceiling to compare the peak against. The "
                "limiter still exists; the number was simply not returned.")

    peak_out = int(stats.get("peak_out") or 0)
    out_used = peak_out / float(otpm)

    itpm = row.get("input_tokens_per_minute")
    in_used = None
    if itpm is not None and itpm > 0:
        in_used = int(stats.get("input_at_peak") or 0) / float(itpm)

    shape = ("peak minute generated %d of an OTPM of %d (%.0f%%)"
             % (peak_out, otpm, out_used * 100))
    shape += (" while input sat at %.0f%% of ITPM" % (in_used * 100)
              if in_used is not None else ", with no ITPM published to compare")

    if out_used >= floor and in_used is not None and in_used >= floor:
        return ("both-limiters-saturated",
                shape + ". Both token limiters are full, so this is volume "
                "rather than shape: caching the prefix helps the input side "
                "and does nothing for the output side, and only batching or a "
                "limit increase moves both.")
    if out_used >= floor:
        return ("otpm-saturated",
                shape + ". The output limiter is what you are hitting, and "
                "there is no cached output, so nothing about the prompt moves "
                "this number.")
    if in_used is not None and in_used >= floor and out_used < watch:
        return ("input-bound",
                shape + ". The input limiter is the one that is full here, not "
                "the output one. Cache reads are not charged against ITPM, so "
                "that is a different finding with a different repair.")
    if out_used >= watch:
        return ("otpm-approaching",
                shape + ". Thin enough that a rise in answer length, or in "
                "thinking effort, lands on the output limiter.")
    return ("otpm-headroom", shape + ".")


def window_start(minutes):
    """Floor to the minute: starting_at must sit on a bucket boundary."""
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    return (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def read_buckets(session, path, params):
    """Walk the paginated usage report."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=int, default=240,
                    help="minutes of one-minute buckets to read (max 1440)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print models with headroom left")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    minutes = max(1, min(int(args.minutes), 1440))
    session = requests.Session()
    session.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    params = {"starting_at": window_start(minutes), "bucket_width": "1m",
              "limit": minutes, "group_by[]": ["model"]}
    stats = peaks(read_buckets(session, "/organizations/usage_report/messages", params))
    if not stats:
        log.info("no message usage in the last %d minute(s)", minutes)
        return 0

    groups = limits_by_group(get(session, "/organizations/rate_limits"))

    checked = 0
    bad = 0
    for model in sorted(stats, key=lambda m: -stats[m]["peak_out"]):
        row = stats[model]
        limits = limits_for(groups, model)
        state, detail = verdict(model, row, limits)
        checked += 1
        line = "%-24s %-28s %s" % (state, model, detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            mean = implied_mean_output(row["peak_out"],
                                       (limits or {}).get("requests_per_minute"))
            if mean is not None:
                log.warning("  RPM would only have bound first at a mean answer "
                            "of %.0f token(s) or shorter, so if your answers are "
                            "longer than that the request rate was never the "
                            "ceiling and more workers add nothing", mean)
            else:
                log.warning("  no requests_per_minute published for this group, "
                            "so the request rate cannot be ruled out from here")
            ratio = output_to_input_ratio(limits)
            if ratio is not None:
                log.warning("  OTPM is %.0f%% of ITPM on this group, so "
                            "generation reaches its ceiling first", ratio * 100)
            log.warning("  repair: move latency tolerant generation to the "
                        "Message Batches API, which has its own limiter group "
                        "and costs half; or lower output_config.effort, since "
                        "thinking tokens are counted as output; or request an "
                        "output_tokens_per_minute increase.")
            log.warning("  repair: do not lower max_tokens. It is documented "
                        "not to factor into OTPM, so it truncates answers "
                        "without buying a single token of headroom.")
        elif state == "input-bound":
            log.warning(line)
            log.warning("  repair: this one is the input limiter. Cache reads "
                        "are not charged against ITPM, so covering the stable "
                        "prefix is the lever there, not anything on this page.")
        elif state in ("otpm-approaching", "no-limit-published"):
            log.warning(line)
        elif args.show_all:
            log.info(line)

    log.info("%d model(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
