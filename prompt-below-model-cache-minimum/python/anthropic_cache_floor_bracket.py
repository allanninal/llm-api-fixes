"""Bracket a cached prefix against the cache minimums of the models it runs on.

Read only. One GET against the Admin API, which needs an Admin API key
(sk-ant-admin...); a workspace key is rejected by every /v1/organizations/
path, and an Admin key can be provisioned read-only.

A prefix shorter than a model's minimum cacheable token count is not cached and
no error is raised for it: cache_control is accepted and ignored. The messages
usage report carries no request count, so the prefix cannot be measured
directly from it. It can be bracketed. One API key that runs several models
sends the same prefix to all of them, so if caching works on the models with a
low minimum and stops dead on the models with a high one, the prefix sits
between the two floors. That bracket is the finding, and it is a size the
report was never asked for.

The repair is printed, never performed.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_cache_floor_bracket")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# Published minimum cacheable prompt length per model family, in tokens. Longest
# prefix wins, so a dated id such as claude-haiku-4-5-20251001 resolves through
# claude-haiku-4-5. A model that is not in this table gets no floor and is left
# out of the verdict rather than guessed at: a wrong floor here would invent a
# bracket that does not exist.
CACHE_MINIMUMS = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-mythos-5": 512,
    "claude-mythos-preview": 2048,
    "claude-opus-4-8": 1024,
    "claude-opus-4-7": 2048,
    "claude-opus-4-6": 4096,
    "claude-opus-4-5": 4096,
    "claude-opus-4-1": 1024,
    "claude-opus-4": 1024,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-sonnet-4-5": 1024,
    "claude-sonnet-4": 1024,
    "claude-haiku-4-5": 4096,
    "claude-haiku-3-5": 2048,
}

FINDINGS = ("below-cache-minimum",)


def _int(value):
    """Read a usage field as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def cache_minimum(model):
    """The model's minimum cacheable prompt length, in tokens. Pure.

    Longest prefix match, because the ids that actually appear in a usage report
    are dated snapshots. None for anything unrecognised, and None has to mean
    "no opinion" everywhere downstream rather than "no floor": a model quietly
    treated as floor zero would land on the caching side of every bracket.
    """
    name = str(model or "").strip().lower()
    if not name:
        return None
    best = None
    for family, floor in CACHE_MINIMUMS.items():
        if name == family or name.startswith(family + "-"):
            if best is None or len(family) > len(best[0]):
                best = (family, floor)
    return best[1] if best else None


def series(buckets):
    """Per (api_key_id, model), the window's token totals. Pure."""
    out = {}
    for bucket in buckets or []:
        for result in bucket.get("results") or []:
            if not isinstance(result, dict):
                continue
            ident = (str(result.get("api_key_id") or "unknown"),
                     str(result.get("model") or "unknown"))
            creation = result.get("cache_creation") or {}
            row = out.setdefault(ident, {"uncached": 0, "writes": 0, "reads": 0})
            row["uncached"] += _int(result.get("uncached_input_tokens"))
            row["writes"] += (_int(creation.get("ephemeral_5m_input_tokens"))
                              + _int(creation.get("ephemeral_1h_input_tokens")))
            row["reads"] += _int(result.get("cache_read_input_tokens"))
    return out


def by_key(totals):
    """Regroup the series into one list of model rows per api_key_id. Pure."""
    out = {}
    for (key, model), row in (totals or {}).items():
        out.setdefault(key, []).append({
            "model": model, "floor": cache_minimum(model),
            "uncached": _int(row.get("uncached")), "writes": _int(row.get("writes")),
            "reads": _int(row.get("reads")),
        })
    for rows in out.values():
        rows.sort(key=lambda r: (r["floor"] if r["floor"] is not None else 10 ** 9,
                                 r["model"]))
    return out


def models_caching_anywhere(totals):
    """Models that cache for at least one key in the org. Pure.

    The cross-key control. If another key caches on the same model, that model
    is not the obstacle and this key's silence is about its own prompt.
    """
    return {model for (_key, model), row in (totals or {}).items()
            if _int(row.get("writes")) + _int(row.get("reads")) > 0}


def split_rows(rows, min_input=100_000):
    """Sort a key's models into caching, silent and unusable. Pure.

    Silent means both cache counters are exactly zero with real input behind
    them. Zero is not a small number here; it is the state cache_control
    produces when the API declines to honour it.
    """
    caching, silent, skipped = [], [], []
    for row in rows or []:
        if row.get("floor") is None:
            skipped.append(row)
        elif _int(row.get("writes")) + _int(row.get("reads")) > 0:
            caching.append(row)
        elif _int(row.get("uncached")) >= min_input:
            silent.append(row)
        else:
            skipped.append(row)
    return caching, silent, skipped


def floor_bracket(caching, silent):
    """Bracket the cached prefix between two floors. Pure. None if it does not.

    lo is the highest floor the key still caches at, hi the lowest floor at
    which it stops. The bracket exists only when the split is clean: every
    silent model sits above every caching one. A silent model beneath a caching
    one is not an eligibility story at all, because the same prompt cleared the
    higher bar.
    """
    if not caching or not silent:
        return None
    lo = max(_int(r.get("floor")) for r in caching)
    hi = min(_int(r.get("floor")) for r in silent)
    if hi <= lo:
        return None
    return (lo, hi)


def handoff(state):
    """Which note owns this shape, when it is not this one. Pure."""
    if state == "no-caching-anywhere":
        return ("this key writes and reads nothing on any model, so there is no "
                "contrast to bracket against. Read the prompt-caching-never-used "
                "note: with no cache_control anywhere the floors are irrelevant.")
    if state == "silent-model-under-a-caching-floor":
        return ("a model with a lower floor is silent while a model with a "
                "higher floor caches, so the prefix cleared the higher bar and "
                "size cannot be the reason. Read the "
                "cache-invalidated-by-changing-prefix note.")
    if state == "peer-caches-same-model":
        return ("another key in this organization caches on this same model, so "
                "the model's floor is not the obstacle. Read the "
                "cache-invalidated-by-changing-prefix note.")
    if state == "single-silent-model":
        return ("one model and no contrast, so this check cannot separate a "
                "prefix under the floor from caching that was never switched "
                "on. Both prompt-caching-never-used and this note remain open. "
                "Route a sample of the traffic through a model with a lower "
                "floor and the ambiguity resolves itself.")
    return ""


def classify(rows, caching_models=(), min_input=100_000):
    """Classify one api_key_id. Pure. Returns (state, detail).

    Only a key that caches under one floor and goes silent above it belongs to
    this note. Everything else is handed away, most of it by name.
    """
    caching, silent, skipped = split_rows(rows, min_input)
    if not caching and not silent:
        return ("too-little-traffic",
                "%d model(s) seen, none with a known floor and enough input to "
                "judge" % len(skipped or []))

    if not caching:
        if len(silent) == 1:
            model = silent[0]["model"]
            if model in set(caching_models or ()):
                return ("peer-caches-same-model",
                        "silent on %s (floor %d) while another key caches on the "
                        "same model" % (model, _int(silent[0]["floor"])))
            return ("single-silent-model",
                    "silent on %s (floor %d) and running nothing else, so there "
                    "is no second floor to bracket against"
                    % (model, _int(silent[0]["floor"])))
        return ("no-caching-anywhere",
                "silent on all %d model(s) with known floors: %s"
                % (len(silent), ", ".join(r["model"] for r in silent)))

    if not silent:
        return ("caches-on-every-model",
                "cache activity on all %d model(s) with known floors" % len(caching))

    bracket = floor_bracket(caching, silent)
    if bracket is None:
        low = min(silent, key=lambda r: _int(r.get("floor")))
        high = max(caching, key=lambda r: _int(r.get("floor")))
        return ("silent-model-under-a-caching-floor",
                "%s (floor %d) is silent while %s (floor %d) caches"
                % (low["model"], _int(low["floor"]), high["model"],
                   _int(high["floor"])))

    lo, hi = bracket
    return ("below-cache-minimum",
            "caching works up to a floor of %d (%s) and stops at %d (%s), so the "
            "cached prefix is at least %d tokens and under %d. cache_control is "
            "being accepted and ignored above the boundary."
            % (lo, ", ".join(r["model"] for r in caching if _int(r["floor"]) == lo),
               hi, ", ".join(r["model"] for r in silent if _int(r["floor"]) == hi),
               lo, hi))


def repair_lines(bracket):
    """The two honest repairs, sized to the bracket. Pure."""
    if not bracket:
        return []
    lo, hi = bracket
    return [
        "move more genuinely stable material in front of the last cache_control "
        "breakpoint until the prefix clears %d tokens: full tool schemas, "
        "few-shot examples, retrieval instructions." % hi,
        "or drop cache_control on the routes above the boundary so the code is "
        "honest about not caching there, and stop budgeting for a discount that "
        "cannot arrive.",
        "do not pad with filler to cross %d. Padding is billed at the full input "
        "rate on the write and only pays back at high repeat volume." % hi,
        "the bracket is %d to %d tokens. If that straddles a route you thought "
        "was much longer, the prefix is being truncated or rebuilt somewhere "
        "before the breakpoint." % (lo, hi),
    ]


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
    ap.add_argument("--days", type=int, default=30,
                    help="days of daily buckets to read (max 90)")
    ap.add_argument("--min-input", type=int, default=100_000,
                    help="uncached input tokens a silent model needs before its "
                         "silence counts as evidence")
    ap.add_argument("--show-all", action="store_true",
                    help="also print keys that are behaving")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key "
                  "(sk-ant-admin...); a workspace key cannot read "
                  "/v1/organizations/")
        return 2

    days = max(2, min(int(args.days), 90))
    session = requests.Session()
    session.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    buckets = read_buckets(session, "/organizations/usage_report/messages", {
        "starting_at": window_start(days),
        "bucket_width": "1d",
        "limit": days + 1,
        "group_by[]": ["model", "api_key_id"],
    })

    totals = series(buckets)
    if not totals:
        log.info("no messages usage in the last %d day(s)", days)
        return 0

    caching_models = models_caching_anywhere(totals)
    keyed = by_key(totals)

    checked = 0
    bad = 0
    for key in sorted(keyed):
        rows = keyed[key]
        state, detail = classify(rows, caching_models, args.min_input)
        checked += 1
        line = "%-32s %s  %s" % (state, key, detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            caching, silent, _ = split_rows(rows, args.min_input)
            for repair in repair_lines(floor_bracket(caching, silent)):
                log.warning("  repair: %s", repair)
            log.warning("  note: the bracket assumes one prefix per key. A key "
                        "that sends a different prompt per model brackets "
                        "nothing, and the report cannot see inside a key.")
        else:
            note = handoff(state)
            if note:
                log.info(line)
                log.info("  %s", note)
            elif args.show_all:
                log.info(line)

    log.info("%d key(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
