"""Probe an endpoint family that is already past its published shutdown date.

Read only. Every request is a GET: the assistants listing, a control listing of
models on the same key, and the organization usage report. Nothing here creates
an assistant, a thread or a run, and a 404 from a listing costs exactly as
little as a 200.

Past a shutdown date the polarity inverts. A 404 is the documented, expected
answer and is not the finding; a 200 is, because it means this organization
still has grace access to an API that is over. A 404 on its own cannot tell a
closed path from a key that reads nothing, so the unit here is a pair: the
subject path against a control path on the same credential, with the path as
the only thing that varies.

The probe measures whether the endpoint answers you today. It cannot date an
outage. That is what the usage report is for, and the two are reported
separately because one is a measurement and the other is an inference.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("assistants_shutdown_probe")

API = "https://api.openai.com/v1"
SUBJECT = "/assistants"
CONTROL = "/models"

# Announced 26 August 2025 with a year of notice; assistants, threads, messages,
# runs and run steps were replaced by the Responses API plus the Conversations
# API. The date is published and not readable -- no endpoint returns it, and
# nothing in GET /v1/models can see a path that no longer exists -- so it is a
# constant here and the note says where it came from.
SHUTDOWN = "2026-08-26"

# Live means the path still routes. A 429 is a refusal from something that
# exists, which is not the same as a 404 from something that does not.
LIVE = ("answering", "throttled")

FINDINGS = ("grace-access", "shut-down", "closed-early", "control-failed",
            "unreadable", "cliff-on-the-date", "dip-on-the-date")

REPAIRS = {
    "grace-access":
        "this organization still reaches an API that shut down on "
        + SHUTDOWN + ". That is grace, not support, and it has no expiry you "
        "can read. Move it now: runs become POST /v1/responses carrying a "
        "conversation id, threads become POST /v1/conversations, and the "
        "OpenAI-Beta header is deleted.",
    "shut-down":
        "runs become POST /v1/responses carrying a conversation id from "
        "POST /v1/conversations, and the OpenAI-Beta: assistants=v2 header is "
        "deleted. There is no model id to swap here, which is why checking the "
        "model id first never helps.",
    "closed-early":
        "the path is already gone and the published date has not arrived. "
        "Treat the date as the outside edge rather than the schedule.",
    "control-failed":
        "the control path did not answer either, so nothing was proved about "
        "the subject path. Fix the credential or the network and re-run before "
        "reading anything else here.",
    "cliff-on-the-date":
        "this project's traffic stopped on the shutdown date, so the outage is "
        "the closure and not a deploy. Migrate this project first: it has the "
        "most to move.",
    "dip-on-the-date":
        "part of this project's traffic stopped on the shutdown date. The "
        "project serves other work as well, so the assistants share is what "
        "needs migrating, not the whole project.",
}


def days_past(today, when=SHUTDOWN):
    """Whole days from a published date to today. Pure. Negative before it."""
    return (dt.date.fromisoformat(str(today))
            - dt.date.fromisoformat(str(when))).days


def probe_state(status, body=None):
    """What one listing's status means on its own. Pure. Returns (state, why).

    On its own is the operative phrase. A 404 from a path that is supposed to
    be gone and a 404 from a key that cannot see it are the same number, and
    only the pair in access_verdict() separates them.
    """
    if status is None:
        return ("unreachable", "no response at all from this path")
    status = int(status)
    body = body if isinstance(body, dict) else {}
    if status == 200:
        kind = body.get("object") or "a body with no object field"
        return ("answering", "200, and the response is %s" % kind)
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = err.get("code") or err.get("type") or "no error code"
    if status == 404:
        return ("gone", "404 %s, which is what a closed path returns" % code)
    if status in (401, 403):
        return ("credentials",
                "%d %s, so this probe says nothing about the path"
                % (status, code))
    if status == 429:
        return ("throttled",
                "429 %s, which is a refusal from a path that still routes"
                % code)
    return ("refused", "%d %s" % (status, code))


def access_verdict(subject, control, past):
    """Grade the subject path against the control path. Pure. (state, why).

    The only function here that looks at both paths at once, and the only one
    entitled to use the word shutdown. Everything upstream of it describes a
    single status code and stops.
    """
    if control not in LIVE:
        return ("control-failed",
                "the control path came back %s, so this key proves nothing "
                "about the subject path" % control)
    if subject in LIVE:
        if past >= 0:
            return ("grace-access",
                    "the subject path answered %d day(s) after its published "
                    "shutdown date, which is access on grace rather than a "
                    "supported state" % past)
        return ("still-open",
                "the subject path answers and the shutdown is %d day(s) away"
                % -past)
    if subject == "gone":
        if past >= 0:
            return ("shut-down",
                    "the control path answers and the subject path does not, "
                    "so this organization is past the %s shutdown" % SHUTDOWN)
        return ("closed-early",
                "the subject path is already gone with %d day(s) still to run "
                "on the published date" % -past)
    return ("unreadable",
            "the subject path came back %s, which is neither an answer nor a "
            "closure" % subject)


def cliff_verdict(series, when=SHUTDOWN):
    """Grade a daily [(date, requests)] series. Pure. Returns (state, why).

    Dates an outage, or declines to. A project that served only assistants
    traffic goes to zero on the date; one that served other work as well shows
    a step down, and a step down is reported as a step down. Rounding the
    second case up to the first is how an inference gets published as a fact.
    """
    rows = sorted((str(d), float(n or 0)) for d, n in (series or []))
    if not rows:
        return ("not-checked",
                "no usage buckets were read, so the outage could not be dated")
    before = [n for d, n in rows if d < str(when)]
    after = [n for d, n in rows if d >= str(when)]
    if not before or not after:
        return ("window-too-short",
                "the window does not span %s, so there is nothing to compare "
                "across it" % when)
    mean_before = sum(before) / len(before)
    mean_after = sum(after) / len(after)
    if mean_before == 0:
        return ("no-traffic-in-window",
                "this project had no requests before %s either, so there is "
                "no outage here to explain" % when)
    if mean_after == 0:
        last_live = max((d for d, n in rows if n > 0), default=None)
        eve = (dt.date.fromisoformat(str(when))
               - dt.timedelta(days=1)).isoformat()
        if last_live == eve:
            return ("cliff-on-the-date",
                    "%.0f requests/day until %s and none from %s, which is the "
                    "shutdown and not a deploy"
                    % (mean_before, last_live, when))
        return ("cliff-elsewhere",
                "traffic stopped, but the last live day is %s rather than %s, "
                "the day before %s" % (last_live, eve, when))
    share = mean_after / mean_before
    if share <= 0.5:
        return ("dip-on-the-date",
                "requests fell to %.0f%% of the prior mean on %s, so part of "
                "this project was assistants traffic and part was not"
                % (share * 100, when))
    return ("still-running",
            "requests continued across %s at %.0f%% of the prior mean"
            % (when, share * 100))


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed."""
    line = REPAIRS.get(state)
    if not line:
        return []
    if state in ("grace-access", "shut-down"):
        return [line,
                "the migration guide is Migrate to the Responses API. There is "
                "no successor model id, so no config change closes this."]
    return [line]


def get_json(session, base, path, key, params=None, timeout=30):
    """One GET. Returns (status, parsed body). Never raises on a 4xx."""
    try:
        r = session.get(base + path, headers={"Authorization": "Bearer " + key},
                        params=params or {}, timeout=timeout)
    except requests.RequestException as exc:
        log.debug("GET %s failed: %s", path, exc)
        return (None, {})
    try:
        return (r.status_code, r.json())
    except ValueError:
        return (r.status_code, {})


def usage_series(session, key, days):
    """{project_id: [(date, requests)]} from the daily usage report."""
    start = int((dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=days)).timestamp())
    params = {"start_time": start, "bucket_width": "1d",
              "group_by[]": ["project_id"], "limit": max(7, min(days, 180))}
    status, body = get_json(session, API, "/organization/usage/completions",
                            key, params)
    if status != 200:
        log.warning("usage report came back %s, so no outage can be dated",
                    status)
        return {}
    out = {}
    for bucket in body.get("data") or []:
        stamp = bucket.get("start_time")
        if not stamp:
            continue
        day = dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc).date().isoformat()
        for row in bucket.get("results") or []:
            pid = row.get("project_id") or "(unattributed)"
            out.setdefault(pid, []).append((day, row.get("num_model_requests") or 0))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of daily usage buckets to read")
    ap.add_argument("--today", default=dt.date.today().isoformat(),
                    help="override the date the arithmetic is done against")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project read key. This script only "
                  "issues GET requests")
        return 2

    past = days_past(args.today)
    log.info("shutdown %s, %d day(s) %s", SHUTDOWN, abs(past),
             "past" if past >= 0 else "away")

    session = requests.Session()
    states = {}
    for role, path in (("control", CONTROL), ("subject", SUBJECT)):
        status, body = get_json(session, API, path, key, {"limit": 1})
        state, why = probe_state(status, body)
        states[role] = state
        emit = log.warning if role == "subject" and state in LIVE else log.info
        emit("  %-8s GET /v1%-12s %s  %-12s %s", role, path,
             "---" if status is None else status, state, why)

    findings = 0
    state, why = access_verdict(states["subject"], states["control"], past)
    emit = log.warning if state in FINDINGS else log.info
    emit("%-20s %s", state, why)
    for line in repair_lines(state):
        emit("  repair: %s", line)
    if state in FINDINGS:
        findings += 1

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.info("%-20s no admin key, so the outage was observed and not dated",
                 "not-dated")
    else:
        series = usage_series(session, admin, args.days)
        if not series:
            log.info("%-20s the usage report returned nothing to date it with",
                     "not-dated")
        for pid, rows in sorted(series.items()):
            state, why = cliff_verdict(rows)
            emit = log.warning if state in FINDINGS else log.info
            emit("%-20s %-18s %s", pid, state, why)
            for line in repair_lines(state):
                emit("  repair: %s", line)
            if state in FINDINGS:
                findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
