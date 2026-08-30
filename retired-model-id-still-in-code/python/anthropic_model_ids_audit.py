"""Find retired Claude model ids still named in your configuration.

Read only. GET requests and nothing else: give this a workspace API key. The
repair is printed, never performed, because this script holds a credential that
can spend real money on inference.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_model_ids_audit")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# Copied from the published deprecations page, because the API has no retirement
# field at all: the model object carries created_at and the token limits and
# nothing about the end of life. A hardcoded table goes stale, so the live list
# from the API always wins over this one; see verdict().
RETIRED = {
    "claude-opus-4-1-20250805": "2026-08-05",
    "claude-opus-4-20250514": "2026-06-15",
    "claude-sonnet-4-20250514": "2026-06-15",
    "claude-3-haiku-20240307": "2026-04-20",
    "claude-3-7-sonnet-20250219": "2026-02-19",
    "claude-3-5-haiku-20241022": "2026-02-19",
    "claude-3-opus-20240229": "2026-01-05",
    "claude-3-5-sonnet-20240620": "2025-10-28",
    "claude-3-5-sonnet-20241022": "2025-10-28",
    "claude-3-sonnet-20240229": "2025-07-21",
    "claude-2.0": "2025-07-21",
    "claude-2.1": "2025-07-21",
    "claude-1.0": "2024-11-06",
    "claude-1.1": "2024-11-06",
    "claude-1.2": "2024-11-06",
    "claude-1.3": "2024-11-06",
    "claude-instant-1.0": "2024-11-06",
    "claude-instant-1.1": "2024-11-06",
    "claude-instant-1.2": "2024-11-06",
}

BAD = ("retired", "unknown", "table-stale", "unreadable")


def replacement(model_id):
    """Where a retired line rolls forward to, by family.

    Family level on purpose. This says the Opus line continues as Opus, not that
    any two snapshots behave the same: a model swap still needs evaluating.
    """
    if "opus" in model_id:
        return "claude-opus-4-8"
    if "haiku" in model_id or "instant" in model_id:
        return "claude-haiku-4-5-20251001"
    if "sonnet" in model_id or model_id.startswith(("claude-1", "claude-2")):
        return "claude-sonnet-4-6"
    return None


def days_since(day_str, today):
    """Whole days from a YYYY-MM-DD string to `today`, or None if unreadable."""
    try:
        return (today - dt.date.fromisoformat(str(day_str))).days
    except (TypeError, ValueError):
        return None


def verdict(model_id, live_ids, today):
    """Classify one model string against the live list and the retirement table.

    Pure: both the live set and the date come in as arguments, so this is
    testable with no network and no clock. Returns (state, detail).

    The live list wins over the table. If the API still lists an id the table
    calls retired, the table is out of date, not the API, and saying so is more
    useful than reporting an outage that is not happening.
    """
    model_id = str(model_id or "").strip()
    if not model_id:
        return ("unreadable", "empty model string")

    retired_on = RETIRED.get(model_id)

    if model_id in live_ids:
        if retired_on:
            return ("table-stale",
                    "still in the live models list, though the local table says "
                    "it retired on %s. Trust the API and correct the table."
                    % (retired_on,))
        return ("live", "in the live models list for this workspace")

    if retired_on:
        ago = days_since(retired_on, today)
        when = ("%s, %d day(s) ago" % (retired_on, ago) if ago is not None
                else retired_on)
        moved_to = replacement(model_id)
        return ("retired",
                "retired on %s. Every request naming it returns 404 "
                "not_found_error, the same body a mistyped id returns.%s"
                % (when, " Line continues as %s." % moved_to if moved_to else ""))

    return ("unknown",
            "not in the live list and not on the deprecation table. That is a "
            "typo, an id that only exists on Bedrock or Vertex (which run later "
            "retirement schedules), or a model this workspace has not been "
            "granted. Three different repairs, so check before assuming.")


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=30)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: check ANTHROPIC_API_KEY; an Admin "
                         "key cannot read the models list" % r.status_code)
    r.raise_for_status()
    return r.json()


def live_model_ids(session):
    """Every id callable by this workspace key, following the cursor."""
    ids, params = set(), {"limit": 1000}
    while True:
        page = get(session, "/models", **params)
        data = page.get("data", [])
        ids.update(str(m.get("id")) for m in data if m.get("id"))
        if not page.get("has_more") or not page.get("last_id"):
            break
        params["after_id"] = page["last_id"]
    return ids


def read_ids(args):
    """Model strings from the command line and, optionally, a file of them."""
    ids = list(args.model)
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    ids.append(line)
    seen, unique = set(), []
    for model_id in ids:
        if model_id not in seen:
            seen.add(model_id)
            unique.append(model_id)
    return unique


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", action="append", default=[],
                    help="a model string found in your code; repeatable")
    ap.add_argument("--from-file",
                    help="file of model strings, one per line, # for comments")
    args = ap.parse_args()

    wanted = read_ids(args)
    if not wanted:
        log.error("give at least one --model, or a --from-file list. Collect "
                  "them with: grep -rn 'claude-' .")
        return 2

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY (a workspace key; this script only "
                  "sends GET requests)")
        return 2

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION})

    live = live_model_ids(session)
    today = dt.date.today()

    counts, bad = {}, 0
    for model_id in wanted:
        state, detail = verdict(model_id, live, today)
        counts[state] = counts.get(state, 0) + 1
        line = "%-12s %s  %s" % (state, model_id or "<empty>", detail)
        if state not in BAD:
            log.info(line)
            continue
        bad += 1
        log.warning(line)
        if state == "retired":
            moved_to = replacement(model_id)
            log.warning("  repair: replace the string %r with %r everywhere it "
                        "appears, including default arguments, fallback "
                        "branches and batch request bodies",
                        model_id, moved_to or "the documented replacement")

    log.info("%d id(s) checked against %d live model(s), %d retired, %d unknown",
             len(wanted), len(live), counts.get("retired", 0),
             counts.get("unknown", 0))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
