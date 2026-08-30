"""Find the day system_fingerprint moved, using completions you already stored.

Read only, and it sends nothing at all. One paged GET of /v1/chat/completions,
which lists chat completions your application created with store set to true.
No completion is created here, which is why this reads somebody else's stored
traffic rather than posting a canary of its own: a canary would generate, would
bill, and would only describe the backend that served one request at the moment
the script ran.

system_fingerprint represents the backend configuration the model runs with and
exists to be read alongside seed, which is documented as best effort rather than
a guarantee. Two distinct values for one model inside the window means any
seed-keyed cache entry or golden file spanning that point is void.

The field is optional. Where it comes back empty on every stored completion for
a model, that is reported as a finding, because a determinism signal you cannot
read is not a determinism signal.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_fingerprint_drift")

LIST_URL = "https://api.openai.com/v1/chat/completions"

MEASURED = ("measured: distinct system_fingerprint values on completions you "
            "already made")
INFERRED = ("inferred: that output recorded before the switch is not "
            "reproducible after it")

FINDINGS = ("fingerprint-moved", "fingerprint-absent", "nothing-stored")


def iso(ts):
    """A UTC timestamp string. Pure. Empty for anything unusable."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def flatten(pages):
    """Rows from listing pages. Pure. A missing fingerprint becomes "".

    Coerced rather than passed through: None and an absent key both mean "no
    fingerprint here", and letting either reach a comparison would make an
    absent value look like a distinct one.
    """
    rows = []
    for page in pages or []:
        for item in (page or {}).get("data") or []:
            if not isinstance(item, dict):
                continue
            try:
                created = int(item.get("created") or 0)
            except (TypeError, ValueError):
                created = 0
            rows.append({"id": str(item.get("id") or ""),
                         "created": created,
                         "model": str(item.get("model") or "(unknown)"),
                         "fingerprint": str(item.get("system_fingerprint") or "")})
    return rows


def within(rows, cutoff):
    """Rows created at or after cutoff. Pure. The clock is passed in."""
    if not cutoff:
        return list(rows or [])
    return [r for r in rows or [] if int(r.get("created") or 0) >= int(cutoff)]


def by_model(rows):
    """{model: [row, ...]} sorted by created. Pure. Order is the whole finding."""
    grouped = {}
    for row in rows or []:
        grouped.setdefault(row.get("model") or "(unknown)", []).append(row)
    for model in grouped:
        grouped[model].sort(key=lambda r: (int(r.get("created") or 0),
                                           r.get("id") or ""))
    return grouped


def transitions(rows):
    """[(created, old, new)] where consecutive fingerprints differ. Pure."""
    out = []
    previous = ""
    for row in rows or []:
        current = str(row.get("fingerprint") or "")
        if not current:
            continue
        if previous and current != previous:
            out.append((int(row.get("created") or 0), previous, current))
        previous = current
    return out


def interleaved(rows):
    """True when a fingerprint reappears after another one. Pure.

    One dated switchover and a fleet serving two configurations at once look
    identical in a set of distinct values and are different problems: the first
    invalidates baselines recorded before a date, the second invalidates the
    idea that two calls this afternoon agree with each other.
    """
    runs = []
    for row in rows or []:
        current = str(row.get("fingerprint") or "")
        if not current:
            continue
        if not runs or runs[-1] != current:
            runs.append(current)
    return len(runs) > len(set(runs))


def verdict(model, rows):
    """Grade one model. Pure. Returns (state, detail)."""
    rows = list(rows or [])
    with_fp = [r for r in rows if r.get("fingerprint")]
    distinct = sorted({r["fingerprint"] for r in with_fp})
    if not rows:
        return ("nothing-stored",
                "no stored completions for %s in this window" % model)
    if not with_fp:
        return ("fingerprint-absent",
                "no stored completion on %s carries a system_fingerprint, so a "
                "backend change cannot be detected here even in principle"
                % model)
    if len(distinct) == 1 and len(with_fp) == 1:
        return ("single-observation",
                "one stored completion on %s carries a fingerprint, which is a "
                "reading and not a comparison" % model)
    if len(distinct) == 1:
        return ("fingerprint-stable",
                "%s ran under one backend configuration across %d stored "
                "completions. seed is documented as best effort, so this is "
                "the parameter behaving rather than a guarantee"
                % (model, len(with_fp)))
    shape = ("interleaving, so more than one configuration is being served at "
             "once" if interleaved(rows) else "switching once")
    return ("fingerprint-moved",
            "%s ran under %d backend configurations in this window, %s"
            % (model, len(distinct), shape))


def repair_lines(state, mixed=False):
    """The repair for one verdict. Pure. Printed, never performed."""
    if state == "fingerprint-moved":
        lines = ["stop using seed as a cache key or a test oracle. Assert on "
                 "structure and semantics, and record system_fingerprint beside "
                 "every baseline so a change explains a diff instead of failing "
                 "a build.",
                 "pin the model snapshot rather than a floating alias, so at "
                 "least the weights are not a second moving part."]
        if mixed:
            lines.append("the values interleave rather than switching once, so "
                         "two calls made minutes apart can land on different "
                         "configurations. Re-recording baselines will not fix "
                         "that; only caching your own responses will.")
        return lines
    if state == "fingerprint-absent":
        return ["do not build reproducibility on seed for this model. There is "
                "no signal to alarm on, so cache your own responses instead.",
                "if a test needs stability, freeze the response in the fixture "
                "rather than asking the platform to reproduce it."]
    if state == "nothing-stored":
        return ["nothing was stored, so this question cannot be answered from "
                "the API. Set store: true on a sample of traffic, or accept "
                "that reproducibility has no evidence behind it.",
                "note that the Responses API object carries neither seed nor "
                "system_fingerprint, and /v1/responses cannot be listed, so a "
                "migration onto it removes this reading entirely."]
    if state == "fingerprint-stable":
        return ["nothing to do today. Keep this run on a schedule: the value "
                "held across the window, which is best effort holding, not a "
                "promise that it will."]
    if state == "single-observation":
        return ["store more traffic or widen the window. One fingerprint is a "
                "reading, and this note needs two to say anything."]
    return []


def fetch(key, model=None, metadata=None, timeout=30):
    """Paged GET of the stored chat completions. Returns (pages, error)."""
    pages = []
    params = {"limit": 100, "order": "asc"}
    if model:
        params["model"] = model
    for pair in metadata or []:
        name, _, value = str(pair).partition("=")
        if name and value:
            params["metadata[%s]" % name.strip()] = value.strip()
    headers = {"Authorization": "Bearer " + key}
    for _ in range(200):
        try:
            r = requests.get(LIST_URL, headers=headers, params=params,
                             timeout=timeout)
        except requests.RequestException as exc:
            return (pages, "request failed: %s" % exc)
        if r.status_code != 200:
            return (pages, "HTTP %d %s" % (r.status_code, (r.text or "")[:160]))
        body = r.json()
        pages.append(body)
        if not body.get("has_more") or not body.get("last_id"):
            break
        params["after"] = body["last_id"]
    return (pages, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="how far back to read, in days")
    ap.add_argument("--model", help="narrow the listing to one model id")
    ap.add_argument("--metadata", action="append", default=[],
                    help="key=value filter, if your calls are tagged")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key set to Read Only. It is "
                  "used for one paged GET of /v1/chat/completions")
        return 2

    pages, err = fetch(key, args.model, args.metadata)
    if err:
        log.error("%s", err)
        return 2

    cutoff = int(time.time()) - args.days * 86400
    rows = within(flatten(pages), cutoff)
    grouped = by_model(rows)
    findings = 0

    if not rows:
        state, detail = verdict("(any model)", [])
        log.warning("%-20s %s", state, detail)
        for line in repair_lines(state):
            log.warning("  repair: %s", line)
        log.info("1 finding(s)")
        return 1

    for model in sorted(grouped):
        entries = grouped[model]
        with_fp = [r for r in entries if r.get("fingerprint")]
        distinct = sorted({r["fingerprint"] for r in with_fp})
        log.info("%-20s %d stored, %d with a fingerprint, %d distinct",
                 model, len(entries), len(with_fp), len(distinct))
        for created, old, new in transitions(entries):
            log.warning("  %s -> %s  at %s", old, new, iso(created))

        state, detail = verdict(model, entries)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-20s %s", state, detail)
        if state == "fingerprint-moved":
            emit("  %s", MEASURED)
            emit("  %s", INFERRED)
        for line in repair_lines(state, interleaved(entries)):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
