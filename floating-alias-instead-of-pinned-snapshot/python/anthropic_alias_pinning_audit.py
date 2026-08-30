"""Report Claude model strings that are aliases rather than pinned snapshots.

Read only. One GET per model string and nothing else: give this a workspace API
key. The repair is printed, never performed, because this script holds a
credential that can spend real money on inference.
"""
import argparse
import datetime as dt
import logging
import os
import re
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_alias_pinning_audit")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# A trailing -YYYYMMDD. Used only to describe an id, never to decide whether it
# is pinned: that answer comes from the API, because from the 4.6 generation on
# a dateless id is itself a snapshot and pattern-matching gets it backwards.
DATED = re.compile(r"-\d{8}$")

BAD = ("alias", "not-found", "unreadable")


def parse_created(value):
    """Read created_at into a date, or None.

    The field is RFC 3339 with a trailing Z, which date.fromisoformat will not
    accept before Python 3.11, so the timestamp is cut at the T rather than
    parsed whole.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw.split("T")[0])
    except ValueError:
        return None


def verdict(requested, model, today):
    """Compare a model string with what GET /v1/models/{id} resolved it to.

    `model` is the returned object, or None for a 404. Pure, and `today` is
    passed in so the age of the resolved snapshot is testable at a fixed date.
    Returns (state, detail).
    """
    requested = str(requested or "").strip()
    if not requested:
        return ("unreadable", "empty model string")

    if model is None:
        return ("not-found",
                "404 not_found_error: nothing resolves this id. If a date "
                "suffix was appended to a 4.6-or-later id, remove it: those "
                "ids are already snapshots and the dated form never existed.")

    resolved = str(model.get("id") or "").strip()
    if not resolved:
        return ("unreadable", "the model object came back with no id")

    created = parse_created(model.get("created_at"))
    age = ("" if created is None else
           " The snapshot behind it was created %s, %d day(s) ago."
           % (created.isoformat(), (today - created).days))

    if resolved != requested:
        return ("alias",
                "an alias: it resolves to %s today, and the pointer moves "
                "without a deploy or an error.%s Pin %s."
                % (resolved, age, resolved))

    if DATED.search(requested):
        return ("pinned", "a dated snapshot; it resolves to itself.%s" % (age,))

    return ("pinned-dateless",
            "already a pinned snapshot even though it carries no date: from the "
            "4.6 generation on, the dateless id is the snapshot. Do not append "
            "a date to it, that id does not exist.%s" % (age,))


def get_model(session, model_id):
    """The model object for one id, or None when the API returns 404."""
    r = session.get("%s/models/%s" % (API, model_id), timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: check ANTHROPIC_API_KEY; an Admin "
                         "key cannot read the models list" % r.status_code)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", action="append", default=[],
                    help="a model string found in your code; repeatable")
    ap.add_argument("--from-file",
                    help="file of model strings, one per line, # for comments")
    args = ap.parse_args()

    wanted = list(args.model)
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    wanted.append(line)
    wanted = list(dict.fromkeys(wanted))
    if not wanted:
        log.error("give at least one --model, or a --from-file list")
        return 2

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY (a workspace key; this script only "
                  "sends GET requests)")
        return 2

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION})

    today = dt.date.today()
    unpinned = 0
    for model_id in wanted:
        state, detail = verdict(model_id, get_model(session, model_id), today)
        line = "%-15s %s  %s" % (state, model_id, detail)
        if state not in BAD:
            log.info(line)
            continue
        if state == "alias":
            unpinned += 1
        log.warning(line)
        if state == "alias":
            log.warning("  repair: write the resolved snapshot into the config "
                        "in place of the alias, record today's mapping beside "
                        "your eval results, then check the new id's retirement "
                        "date")

    log.info("%d id(s) checked, %d unpinned alias(es)", len(wanted), unpinned)
    return 1 if unpinned else 0


if __name__ == "__main__":
    sys.exit(main())
