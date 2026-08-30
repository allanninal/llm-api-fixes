"""Report OpenAI model ids whose published shutdown date has already passed.

Read only. One GET request, no writes: give this a project key set to Read Only.
The repair is printed, never performed, because this script holds a credential
that can spend real money on inference.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_model_shutdown_audit")

API = "https://api.openai.com/v1"

# Printed beside a dead id so the reader is not sent back to the deprecations
# page for the obvious part. Matched longest prefix first, and deliberately
# family-level: this says where a line went, not that any one snapshot is a
# drop-in replacement for another.
SUCCESSORS = (
    ("gpt-image-1", "gpt-image-2"),
    ("chatgpt-image", "gpt-image-2"),
    ("dall-e", "gpt-image-2"),
    ("gpt-5-nano", "gpt-5.6-luna"),
    ("gpt-5-mini", "gpt-5.6-terra"),
    ("gpt-5-pro", "gpt-5.6-sol"),
    ("gpt-5", "gpt-5.6-sol"),
    ("o4-mini", "gpt-5.6-terra"),
    ("o3-pro", "gpt-5.6-sol"),
    ("o3", "gpt-5.6-sol"),
    ("o1", "gpt-5.6-sol"),
    ("gpt-4", "gpt-5.6-sol"),
)

FAILING = ("retired", "retiring-today")


def successor(model_id):
    """The family a retired id was folded into, or None if this script has no
    opinion. An unknown id is left without a suggestion rather than pointed at
    a guess."""
    for prefix, replacement in SUCCESSORS:
        if model_id.startswith(prefix):
            return replacement
    return None


def parse_day(value):
    """Read a shutdown_date into a date, or None when it cannot be read.

    The field is a plain YYYY-MM-DD string. A full timestamp is tolerated by
    taking the date part. Anything else returns None rather than a guess,
    because a guess here either invents an outage or hides one.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw.split("T")[0])
    except ValueError:
        return None


def verdict(model, today):
    """Classify one entry from GET /v1/models against a date you pass in.

    Pure, so the boundary cases can be tested at a fixed date instead of at
    whatever day the suite happens to run. Returns (state, detail).
    """
    model_id = str(model.get("id") or "").strip()
    if not model_id:
        return ("unreadable", "entry has no id field")

    raw = model.get("shutdown_date")
    if raw is None or str(raw).strip() == "":
        return ("open",
                "no shutdown date published. That is the current state of the "
                "field, not a guarantee: re-read it on a schedule.")

    day = parse_day(raw)
    if day is None:
        return ("unreadable-date",
                "shutdown_date is %r, which is not a date this script will "
                "guess at. Check it by hand." % (raw,))

    days = (day - today).days
    if days < 0:
        return ("retired",
                "shut down on %s, %d day(s) ago. Calls naming this id return "
                "404 model_not_found, which is the same error a misspelled "
                "model name returns." % (day.isoformat(), -days))
    if days == 0:
        return ("retiring-today",
                "shuts down today (%s). Requests may already be failing; treat "
                "this as an outage in progress, not a warning."
                % (day.isoformat(),))
    return ("scheduled",
            "shuts down on %s, %d day(s) from now. Still routable today."
            % (day.isoformat(), days))


def get(session, path):
    r = session.get(API + path, timeout=30)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI: the key is wrong, revoked, or belongs "
                         "to another organization")
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", action="append", default=[],
                    help="only report this id; repeatable. Pass the ids your "
                         "code actually names to keep the report about you")
    ap.add_argument("--show-all", action="store_true",
                    help="also print ids that are fine")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY (a project key set to Read Only)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    models = get(session, "/models").get("data", [])
    if not models:
        log.info("the models list came back empty for this key")
        return 0

    wanted = set(args.model)
    if wanted:
        listed = {str(m.get("id") or "") for m in models}
        for missing in sorted(wanted - listed):
            log.warning("%-15s %s  not in the models list at all, so there is no "
                        "shutdown_date left to read. An id that has been dropped "
                        "from the list is already gone.", "absent", missing)
        models = [m for m in models if str(m.get("id") or "") in wanted]

    today = dt.date.today()
    bad = 0
    for model in sorted(models, key=lambda m: str(m.get("id") or "")):
        state, detail = verdict(model, today)
        model_id = str(model.get("id") or "?")
        line = "%-15s %s  %s" % (state, model_id, detail)
        if state in FAILING:
            bad += 1
            log.warning(line)
            replacement = successor(model_id)
            if replacement:
                log.warning("  repair: change model=%r to model=%r at every call "
                            "site, then read shutdown_date on the new id",
                            model_id, replacement)
            else:
                log.warning("  repair: take the replacement from the "
                            "deprecations page and pin it")
        elif state in ("unreadable", "unreadable-date"):
            log.warning(line)
        elif args.show_all or state == "scheduled":
            log.info(line)

    log.info("%d model id(s) checked, %d past their shutdown date",
             len(models), bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
