"""Align a collapse in cache-read share with the day a new model id appeared.

Read only. One GET against the Admin API, which needs an Admin API key
(sk-ant-admin...); a workspace key is rejected by every /v1/organizations/
path, and an Admin key can be provisioned read-only.

Caches are keyed per model, so the first day on a new model is cold by
definition and a note that fires on it is wrong. What matters is what happens
on the days after. This finds the single largest step down in the daily
cache-read share anywhere in the window, and then asks whether that step sits
where the new model id first appears. A collapse that lines up with the switch
is the switch; a collapse three weeks either side of it is something else, and
this says so rather than taking the credit.

The repair is printed, never performed.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_cache_step_after_model_switch")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# Published minimum cacheable prompt length per model family, in tokens. Only
# used to explain a confirmed step, never to make one: a migration from a 512
# floor to a 4,096 floor is the most common reason the share never comes back.
CACHE_MINIMUMS = {
    "claude-opus-5": 512, "claude-fable-5": 512, "claude-mythos-5": 512,
    "claude-mythos-preview": 2048, "claude-opus-4-8": 1024,
    "claude-opus-4-7": 2048, "claude-opus-4-6": 4096, "claude-opus-4-5": 4096,
    "claude-opus-4-1": 1024, "claude-opus-4": 1024, "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024, "claude-sonnet-4-5": 1024, "claude-sonnet-4": 1024,
    "claude-haiku-4-5": 4096, "claude-haiku-3-5": 2048,
}

FINDINGS = ("collapsed-after-model-change",)


def _int(value):
    """Read a usage field as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def cache_minimum(model):
    """The model's minimum cacheable prompt length. Pure. None if unrecognised."""
    name = str(model or "").strip().lower()
    if not name:
        return None
    best = None
    for family, floor in CACHE_MINIMUMS.items():
        if name == family or name.startswith(family + "-"):
            if best is None or len(family) > len(best[0]):
                best = (family, floor)
    return best[1] if best else None


def day_key(stamp):
    """Normalise a timestamp to a UTC day. Pure. None if unreadable."""
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        try:
            when = dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
        return when.strftime("%Y-%m-%d")
    text = str(stamp or "").strip().replace(" ", "T")
    if len(text) < 10:
        return None
    head = text[:10]
    if head[4] != "-" or head[7] != "-":
        return None
    for part in (head[0:4], head[5:7], head[8:10]):
        if not part.isdigit():
            return None
    return head


def daily_rows(buckets):
    """One row per day that carried input, sorted. Pure.

    Days with no traffic are left out rather than zero-filled. A zero-share day
    invented for a weekend would be the largest step in most windows and would
    then have to be explained away by every branch below it.
    """
    merged = {}
    for bucket in buckets or []:
        day = day_key(bucket.get("starting_at") or bucket.get("start_time"))
        if day is None:
            continue
        for result in bucket.get("results") or []:
            if not isinstance(result, dict):
                continue
            model = str(result.get("model") or "unknown")
            creation = result.get("cache_creation") or {}
            row = merged.setdefault(day, {"day": day, "uncached": 0, "reads": 0,
                                          "writes": 0, "by_model": {}})
            uncached = _int(result.get("uncached_input_tokens"))
            reads = _int(result.get("cache_read_input_tokens"))
            writes = (_int(creation.get("ephemeral_5m_input_tokens"))
                      + _int(creation.get("ephemeral_1h_input_tokens")))
            row["uncached"] += uncached
            row["reads"] += reads
            row["writes"] += writes
            row["by_model"][model] = (row["by_model"].get(model, 0)
                                      + uncached + reads + writes)
    rows = [r for r in merged.values() if r["uncached"] + r["reads"] > 0]
    rows.sort(key=lambda r: r["day"])
    for position, row in enumerate(rows):
        row["position"] = position
        row["share"] = row["reads"] / float(row["reads"] + row["uncached"])
    return rows


def arrival_positions(rows):
    """Models that first appear after the window opens. Pure.

    A model present on day one might have been running for a year, so its
    "arrival" is an artefact of where the window starts and it is excluded.
    """
    first = {}
    for row in rows or []:
        for model in (row.get("by_model") or {}):
            first.setdefault(model, _int(row.get("position")))
    return {model: position for model, position in first.items() if position > 0}


def input_share_after(rows, model, position):
    """Fraction of input on one model from a position onward. Pure. None if idle.

    The guard against blaming a model nobody uses. A canary taking one percent
    of traffic cannot move an organization-wide ratio, and a note that lets it
    take the blame will point at the wrong deploy every time.
    """
    total = 0
    mine = 0
    for row in rows or []:
        if _int(row.get("position")) < position:
            continue
        for name, tokens in (row.get("by_model") or {}).items():
            total += _int(tokens)
            if name == model:
                mine += _int(tokens)
    if total <= 0:
        return None
    return mine / float(total)


def step_at(shares, position, min_side=3):
    """The step across one position, with that day itself left out. Pure.

    Returns (before, after, delta) or Nones. Excluding the day is the whole
    care in this function: a new model's first day is cold because the cache is
    empty, which is correct behaviour, and averaging it into either side turns
    an expected cold start into a finding.
    """
    shares = list(shares or [])
    before = shares[:position]
    after = shares[position + 1:]
    if len(before) < min_side or len(after) < min_side:
        return (None, None, None)
    b = sum(before) / float(len(before))
    a = sum(after) / float(len(after))
    return (b, a, b - a)


def best_split(shares, min_side=3):
    """The largest downward step anywhere in the series. Pure.

    Returns (position, delta) or (None, None). This is what makes the alignment
    claim falsifiable: without it, any window containing both a new model and a
    decline reads as causation. With it, the decline has to be biggest at the
    switch and nowhere else.
    """
    shares = list(shares or [])
    n = len(shares)
    if n < min_side * 2:
        return (None, None)
    best_position, best_delta = None, None
    for position in range(min_side, n - min_side + 1):
        b = sum(shares[:position]) / float(position)
        a = sum(shares[position:]) / float(n - position)
        delta = b - a
        if best_delta is None or delta > best_delta:
            best_position, best_delta = position, delta
    return (best_position, best_delta)


def sustained(shares, position, min_side=3):
    """True when every day after the switch sits below every day before. Pure.

    A dip that recovers is a deploy that was rolled back, or a cache warming up
    over a few days. Only a floor that never comes back is structural.
    """
    shares = list(shares or [])
    before = shares[:position]
    after = shares[position + 1:]
    if len(before) < min_side or len(after) < min_side:
        return False
    return max(after) < min(before)


def floor_note(old_model, new_model):
    """Why the share might not come back, when the floors explain it. Pure."""
    old_floor = cache_minimum(old_model)
    new_floor = cache_minimum(new_model)
    if old_floor is None or new_floor is None:
        return ""
    if new_floor > old_floor:
        return ("%s needs %d tokens before a prefix is cacheable and %s needed "
                "%d, so a prompt that has not changed can have stopped "
                "qualifying. That is the prompt-below-model-cache-minimum note, "
                "and it is the most likely mechanism here."
                % (new_model, new_floor, old_model, old_floor))
    return ("%s has the same or a lower cache minimum (%d) as %s (%d), so the "
            "floor does not explain this. Look at thinking or effort defaults "
            "and at the tokenizer instead."
            % (new_model, new_floor, old_model, old_floor))


def handoff(state):
    """Which note owns this shape, when it is not this one. Pure."""
    if state == "no-new-model":
        return ("no model id appeared for the first time in this window, so "
                "nothing here can be attributed to a switch. If the share is "
                "low, read cache-invalidated-by-changing-prefix and "
                "prompt-caching-never-used.")
    if state == "step-elsewhere":
        return ("the largest step in the series is not where the new model "
                "arrived, so something else changed on that day. Read the "
                "cache-invalidated-by-changing-prefix note and line the step up "
                "against your deploys.")
    if state == "expected-cold-start":
        return ("the share dropped on the switch day and came back. That is a "
                "cold cache filling up, which is what a model change is "
                "supposed to cost, and it is not a finding.")
    return ""


def classify(rows, min_days=14, min_drop=0.15, ratio_floor=0.6,
             min_migration=0.20, min_side=3):
    """Classify one window. Pure. Returns (state, detail)."""
    rows = rows or []
    if len(rows) < min_days:
        return ("too-few-days",
                "%d day(s) with input in the window, under the floor of %d"
                % (len(rows), min_days))

    shares = [r["share"] for r in rows]
    arrivals = arrival_positions(rows)
    if not arrivals:
        return ("no-new-model",
                "every model id in this window was already present on day one")

    ranked = sorted(arrivals.items(),
                    key=lambda item: input_share_after(rows, item[0], item[1]) or 0.0,
                    reverse=True)
    model, position = ranked[0]
    migration = input_share_after(rows, model, position) or 0.0
    if migration < min_migration:
        return ("new-model-marginal",
                "%s arrived on %s but carries only %.0f%% of input since, under "
                "the floor of %.0f%%. Too small to move the ratio."
                % (model, rows[position]["day"], migration * 100,
                   min_migration * 100))

    before, after, delta = step_at(shares, position, min_side)
    if delta is None:
        return ("window-too-short-around-the-switch",
                "%s arrived on %s with fewer than %d day(s) either side of it"
                % (model, rows[position]["day"], min_side))

    # Alignment is checked before magnitude, and only against a step that is
    # material on its own. A big fall somewhere else in the window disqualifies
    # the switch outright, however the numbers either side of the switch read.
    peak, peak_delta = best_split(shares, min_side)
    if (peak is not None and peak_delta is not None and peak_delta >= min_drop
            and abs(peak - position) > 1):
        return ("step-elsewhere",
                "the share falls hardest at %s, not at the %s switch on %s"
                % (rows[peak]["day"], model, rows[position]["day"]))

    if delta < min_drop or after > before * ratio_floor:
        if before - shares[position] >= min_drop:
            return ("expected-cold-start",
                    "%s arrived on %s, the share dipped to %.0f%% that day and "
                    "settled back at %.0f%% against %.0f%% before"
                    % (model, rows[position]["day"], shares[position] * 100,
                       after * 100, before * 100))
        return ("steady",
                "%s arrived on %s and the share held at %.0f%% against %.0f%% "
                "before" % (model, rows[position]["day"], after * 100,
                            before * 100))

    if peak is None or abs(peak - position) > 1:
        return ("step-elsewhere",
                "the share falls hardest at %s, not at the %s switch on %s"
                % (rows[peak]["day"] if peak is not None else "no single day",
                   model, rows[position]["day"]))

    if not sustained(shares, position, min_side):
        return ("partial-recovery",
                "%.0f%% before the %s switch and %.0f%% after, but some days "
                "since have recovered above the pre-switch floor. Suggestive "
                "and not conclusive: widen the window."
                % (before * 100, model, after * 100))

    return ("collapsed-after-model-change",
            "cache-read share %.0f%% before %s arrived on %s and %.0f%% after, "
            "with the switch day itself excluded. %s now carries %.0f%% of "
            "input and the largest step in the window is exactly there."
            % (before * 100, model, rows[position]["day"], after * 100, model,
               migration * 100))


def previous_model(rows, position):
    """The model carrying the most input before the switch. Pure."""
    totals = {}
    for row in rows or []:
        if _int(row.get("position")) >= position:
            continue
        for name, tokens in (row.get("by_model") or {}).items():
            totals[name] = totals.get(name, 0) + _int(tokens)
    if not totals:
        return None
    return max(totals.items(), key=lambda item: item[1])[0]


def repair_lines(old_model, new_model):
    """What to check about the new model, in the order that pays. Pure."""
    lines = []
    note = floor_note(old_model, new_model)
    if note:
        lines.append(note)
    lines.extend([
        "compare the two models' minimum cacheable token counts and move the "
        "cache_control breakpoint so the prefix clears the higher one.",
        "compare their thinking and effort defaults. Those are model-specific "
        "and they sit inside the cached prefix, so a different default is a "
        "different prefix.",
        "count the prefix again under the new model id. A newer tokenizer can "
        "produce materially more tokens for the same text, which moves a prefix "
        "that used to sit just above a boundary.",
        "then re-measure the cache-read share over the following three days, "
        "not the following one. The first day after any breakpoint change is "
        "cold for the same reason the switch day was.",
    ])
    return lines


def window_start(days):
    """Floor to the day: starting_at has to sit on a bucket boundary."""
    now = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0,
                                                   microsecond=0)
    return (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/ needs an Admin "
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
    ap.add_argument("--days", type=int, default=31,
                    help="days of daily buckets to read (max 90)")
    ap.add_argument("--min-drop", type=float, default=0.15,
                    help="fall in cache-read share, in share points, that counts "
                         "as a step")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key "
                  "(sk-ant-admin...); a workspace key cannot read "
                  "/v1/organizations/")
        return 2

    days = max(14, min(int(args.days), 90))
    session = requests.Session()
    session.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    buckets = read_buckets(session, "/organizations/usage_report/messages", {
        "starting_at": window_start(days),
        "bucket_width": "1d",
        "limit": days + 1,
        "group_by[]": ["model"],
    })

    rows = daily_rows(buckets)
    if not rows:
        log.info("no messages usage in the last %d day(s)", days)
        return 0

    state, detail = classify(rows, min_drop=args.min_drop)
    line = "%-32s %s" % (state, detail)

    if state in FINDINGS:
        log.warning(line)
        arrivals = arrival_positions(rows)
        ranked = sorted(arrivals.items(),
                        key=lambda item: input_share_after(rows, item[0], item[1]) or 0.0,
                        reverse=True)
        model, position = ranked[0]
        for repair in repair_lines(previous_model(rows, position), model):
            log.warning("  repair: %s", repair)
        log.warning("  note: this is an organization-wide ratio. A second "
                    "workload that changed on the same day would be folded into "
                    "it, so line the date up against a deploy before acting.")
        log.info("%d day(s) checked, 1 finding(s)", len(rows))
        return 1

    note = handoff(state)
    log.info(line)
    if note:
        log.info("  %s", note)
    log.info("%d day(s) checked, 0 finding(s)", len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
