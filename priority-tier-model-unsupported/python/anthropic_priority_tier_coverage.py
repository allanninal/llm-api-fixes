"""Find Claude models that never report Priority Tier service.

Read only. One paged GET against the messages usage report with an Admin API
key. Nothing is sent to /v1/messages and no request body is constructed.

The finding is coverage, not misconfiguration: Priority Tier is not supported on
every model id, so a migration to a newer model can end priority routing with no
error and no diff. The absence is only visible in the usage report grouped by
service_tier, because Priority Tier costs are excluded from the cost report.

No dollar figure is printed anywhere. There is no read-only source for the money
on this surface, and a number derived from a published rate would be a guess
wearing the clothes of a reading.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_priority_tier_coverage")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

PRIORITY = "priority"
STANDARD = "standard"
BATCH = "batch"
UNKNOWN = "unknown"
TIERS = (PRIORITY, STANDARD, BATCH, UNKNOWN)

# Documented as NOT supported by Priority Tier. Matched as a family fragment
# rather than an exact id, because ids carry date suffixes. The fragments are
# written with their leading hyphen so that "opus-5" cannot match
# "claude-opus-4-5", which is a supported model and would otherwise be
# condemned by a careless substring test.
UNSUPPORTED_FAMILIES = ("-opus-5", "-sonnet-5", "-mythos-5", "-mythos-preview")

# Burndown multipliers against a commitment. Named in the output, never applied:
# the script cannot see which tokens carried which attribute without regrouping
# the entire report, and applying an average would be worse than saying nothing.
BURNDOWN = ("cache reads 0.1x", "5-minute cache writes 1.25x",
            "1-hour cache writes 2.0x", "inference_geo us 1.1x on 4.6+")

FINDINGS = ("unsupported-model", "uncovered-model", "partial-priority")


def tier(result):
    """Normalise the service_tier on one result row. Pure.

    An absent or unrecognised value becomes "unknown" and never "standard".
    Folding unclassified traffic into standard inflates the standard share,
    which is the direction that makes a coverage gap look worse than it is.
    """
    raw = str((result or {}).get("service_tier") or "").strip().lower()
    return raw if raw in (PRIORITY, STANDARD, BATCH) else UNKNOWN


def weigh(result):
    """Total billed tokens on one result row. Pure.

    cache_creation is an object rather than a scalar, so a reader that treats it
    as an int silently drops every cached write from the weight and understates
    the models that cache the most.
    """
    row = result or {}
    total = 0
    for field in ("uncached_input_tokens", "cache_read_input_tokens",
                  "output_tokens"):
        try:
            total += int(row.get(field) or 0)
        except (TypeError, ValueError):
            pass
    creation = row.get("cache_creation")
    if isinstance(creation, dict):
        for value in creation.values():
            try:
                total += int(value or 0)
            except (TypeError, ValueError):
                pass
    return total


def fold(pages):
    """Sum tokens into {model: {tier: tokens}}. Pure."""
    out = {}
    for page in pages or []:
        for bucket in (page or {}).get("data") or []:
            for result in (bucket or {}).get("results") or []:
                model = str((result or {}).get("model") or "all models")
                row = out.setdefault(model, {t: 0 for t in TIERS})
                row[tier(result)] += weigh(result)
    return out


def is_unsupported(model):
    """Is this model id on the documented Priority Tier exclusion list? Pure.

    Matched on the hyphenated family fragment. claude-opus-4-5 and
    claude-haiku-4-5 are supported and must not match; claude-opus-5 and
    claude-sonnet-5-20260101 must.
    """
    name = "-" + str(model or "").strip().lower().lstrip("-")
    return any(fragment in name for fragment in UNSUPPORTED_FAMILIES)


def org_has_priority(rows):
    """Does any model in the window report priority tokens? Pure.

    Run before any model is graded. Priority Tier capacity can no longer be
    bought, so an organization with no commitment reports zero everywhere, and
    a per-model coverage verdict in that organization would be a finding about
    nothing.
    """
    return any(int((row or {}).get(PRIORITY) or 0) > 0
               for row in (rows or {}).values())


def share(row, which):
    """One tier's share of a model's billed tokens. Pure. 0.0 when empty."""
    data = row or {}
    total = sum(int(data.get(t) or 0) for t in TIERS)
    if total <= 0:
        return 0.0
    return int(data.get(which) or 0) / float(total)


def verdict(model, row, has_priority, min_tokens=1_000_000, thin=0.60):
    """Classify one model's tier coverage. Pure. Returns (state, detail)."""
    data = row or {}
    total = sum(int(data.get(t) or 0) for t in TIERS)
    if total < min_tokens:
        return ("low-volume",
                "%d billed token(s) in the window, too few to conclude anything"
                % total)

    if not has_priority:
        return ("no-priority-in-org",
                "0%% priority of %.1fM token(s), and no model in this "
                "organization reports priority either. That is an organization "
                "without a capacity commitment, not a gap on this model."
                % (total / 1e6))

    got = share(data, PRIORITY)
    if got <= 0:
        if is_unsupported(model):
            return ("unsupported-model",
                    "0%% priority of %.1fM token(s). Documented as not "
                    "supported by Priority Tier, so service_tier auto is "
                    "accepted here and served standard every time."
                    % (total / 1e6))
        return ("uncovered-model",
                "0%% priority of %.1fM token(s), and this id is not on the "
                "documented exclusion list. Something else is keeping it off "
                "the tier: standard_only on the request, a workspace outside "
                "the commitment, or capacity that never had headroom."
                % (total / 1e6))
    if got < thin:
        return ("partial-priority",
                "%.0f%% priority of %.1fM token(s). Eligible, and mostly over "
                "the committed tokens per minute, so the rest fell back to "
                "standard." % (got * 100, total / 1e6))
    return ("priority-covered",
            "%.0f%% priority of %.1fM token(s)" % (got * 100, total / 1e6))


def repair_lines(state, model):
    """The repair for one classified model. Pure. Printed, never performed."""
    if state == "unsupported-model":
        return [
            "this is coverage, not configuration: %s cannot be served on "
            "Priority Tier at all, whatever service_tier says." % model,
            "either move the latency-sensitive traffic to a covered model id, "
            "or accept standard here and stop planning around a tier that "
            "never applies to it.",
            "standard_only is the way to deliberately preserve commitment "
            "capacity for the models that can use it.",
        ]
    if state == "uncovered-model":
        return [
            "check the request side for standard_only, and check that the "
            "workspace sending this traffic is inside the commitment.",
            "the exclusion list is not the explanation for %s, so the answer "
            "is in your own configuration or in capacity." % model,
        ]
    if state == "partial-priority":
        return [
            "the commitment is sized below this traffic. Requests past the "
            "committed input and output tokens per minute fall back to "
            "standard automatically, and one that would breach the ordinary "
            "rate limits is declined rather than served.",
            "burndown against the commitment is not one token per token: %s."
            % ", ".join(BURNDOWN),
        ]
    return []


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params):
    """Walk the paginated usage report."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        yield page
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def window_start(days):
    """Floor to midnight UTC: starting_at must sit on a bucket boundary."""
    now = dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of daily buckets to read (default 30)")
    ap.add_argument("--min-tokens", type=int, default=1_000_000,
                    help="billed tokens below which no claim is made")
    ap.add_argument("--thin", type=float, default=0.60,
                    help="priority share below which coverage is called partial")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    rows = fold(pages(s, "/organizations/usage_report/messages",
                      {"starting_at": window_start(args.days),
                       "bucket_width": "1d", "limit": min(args.days + 1, 31),
                       "group_by[]": ["service_tier", "model"]}))
    if not rows:
        log.info("no usage in the last %d day(s)", args.days)
        return 0

    has_priority = org_has_priority(rows)
    covered = sum(1 for row in rows.values() if int(row.get(PRIORITY) or 0) > 0)
    if has_priority:
        log.info("org has priority traffic on %d of %d model(s), so a per-model "
                 "zero is meaningful", covered, len(rows))
    else:
        log.warning("no model in this organization reported any priority "
                    "token(s) in the window. Capacity commitments are no longer "
                    "available to purchase, so this is an organization without "
                    "one rather than a gap on any single model.")

    bad = 0
    for model in sorted(rows, key=lambda m: -sum(rows[m].values())):
        state, detail = verdict(model, rows[model], has_priority,
                                args.min_tokens, args.thin)
        line = "%-20s %-26s %s" % (state, model, detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
            for repair in repair_lines(state, model):
                log.warning("  repair: %s", repair)
        else:
            log.info(line)

    log.info("%d model(s) checked, %d finding(s)", len(rows), bad)
    log.info("no dollar figure: Priority Tier costs are excluded from the cost "
             "report, so tokens are the only read-only reading available here")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
