"""Grade two verbs on one resource: creating a fine-tuning job, and serving one.

Read only. Every request is a GET: the job listing, the organization usage
report, and the model objects for each fine-tune and its base. Nothing here
submits a job. That matters more than usual: the obvious way to find out
whether creation is still accepted is to attempt one, and attempting one spends
money, trains a model nobody asked for, and is a write.

So eligibility is computed from readable state instead. Three inputs decide it,
all of them readable with the keys this script already holds: the date, whether
the job list is non-empty, and how long it has been since any ft: prefixed
model produced a request.

The serving side is a separate clock and lands first. Every fine-tunable base
in the deprecation table shuts down 2026-10-23, before the create cutoff
arrives, so the two deadlines cross in the worst order.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fine_tuning_gate_audit")

API = "https://api.openai.com/v1"

# Announced 7 May 2026, in three stages. The middle one is the only rule here
# that is not a date: it is a rolling 60 day window over your own traffic.
NEVER_FINE_TUNED = "2026-05-07"   # never fine-tuned before: cannot create
NO_RECENT_INFERENCE = "2026-07-02"  # no ft: inference in 60 days: cannot create
CUTOFF = "2027-01-06"             # nobody can create
WINDOW = 60                       # days of ft: inference the middle rule wants

# Inference on a fine-tune dies with its base, and every fine-tunable base in
# the deprecation table shuts down on this date. Used only as the fallback when
# the model object itself carries no shutdown_date, and labelled as such.
BASE_SHUTDOWN = "2026-10-23"

# The deprecation table's six fine-tuned families and their replacements. The
# match is exact or hyphen-delimited, never a loose prefix: gpt-4.1-nano starts
# with the characters gpt-4 and must not be filed under ft-gpt-4 with the wrong
# replacement. A base the table does not cover comes back unknown rather than
# as the nearest-looking row.
FAMILIES = (
    ("gpt-3.5-turbo", "ft-gpt-3.5-turbo", "gpt-5.6-terra"),
    ("gpt-4.1-nano-2025-04-14", "ft-gpt-4.1-nano-2025-04-14", "gpt-5.6-luna"),
    ("gpt-4", "ft-gpt-4", "gpt-5.6-sol"),
    ("babbage-002", "ft-babbage-002", "gpt-5.6-terra"),
    ("davinci-002", "ft-davinci-002", "gpt-5.6-terra"),
    ("o4-mini-2025-04-16", "ft-o4-mini-2025-04-16", "gpt-5.6-terra"),
)

FINDINGS = ("blocked-never-fine-tuned", "blocked-no-recent-inference",
            "eligibility-expiring", "create-closed", "unknown-eligibility",
            "already-dead", "dying-soon", "no-base-date")

REPAIRS = {
    "blocked-never-fine-tuned":
        "this organization has no fine-tuning history and the "
        + NEVER_FINE_TUNED + " restriction has passed, so creation is already "
        "refused. Nothing reopens that.",
    "blocked-no-recent-inference":
        "route real traffic to a fine-tune to reopen the window, or accept "
        "that this organization is out of the fine-tuning business as of "
        + NO_RECENT_INFERENCE + ".",
    "eligibility-expiring":
        "the 60 day window is closing. Either retrain now, while creating a "
        "job is still permitted, or keep a real workload on a fine-tuned model "
        "so the clock does not run out on a quiet week.",
    "create-closed":
        "the " + CUTOFF + " cutoff has passed and no organization can create "
        "a fine-tuning job. Whatever is still serving is the last of it.",
    "unknown-eligibility":
        "the inference clock could not be read, so eligibility is unknown "
        "rather than fine. Re-run with an admin-read key before planning "
        "around it.",
    "already-dead":
        "the base is past its shutdown date, so this fine-tune has stopped "
        "serving. Retraining onto a supported base is the only route back, and "
        "it is only available until " + CUTOFF + ".",
    "dying-soon":
        "retrain onto the supported base before the date. Where the fine-tune "
        "only ever encoded formatting, evaluate replacing it with prompting "
        "plus structured outputs instead of retraining at all.",
    "no-base-date":
        "neither the model object nor the published table has a date for this "
        "base, so its serving deadline is unknown. Treat it as undated rather "
        "than as safe.",
}


def days_left(today, when):
    """Whole days from today to a date. Pure. Negative once it has passed."""
    return (dt.date.fromisoformat(str(when))
            - dt.date.fromisoformat(str(today))).days


def create_eligibility(today, has_prior_jobs, days_since_ft_inference):
    """Can this organization still create a job? Pure. (state, detail).

    Three readable inputs and nothing else. days_since_ft_inference is an int,
    the string "none-in-window" when the usage window held no ft: traffic at
    all, or None when it could not be read. There is a test that a blocked
    verdict comes out of these alone, because the alternative way to answer
    this question is to submit a job, and this script never will.
    """
    if days_left(today, CUTOFF) < 0:
        return ("create-closed",
                "the %s cutoff has passed, so no organization can create a "
                "fine-tuning job" % CUTOFF)
    if not has_prior_jobs and days_left(today, NEVER_FINE_TUNED) < 0:
        return ("blocked-never-fine-tuned",
                "the job list is empty and the %s restriction has passed, so "
                "this organization cannot create a job today. Read from the "
                "listing, not from an attempt" % NEVER_FINE_TUNED)
    if days_left(today, NO_RECENT_INFERENCE) >= 0:
        return ("eligible",
                "the 60 day inference rule does not apply until %s; %d day(s) "
                "until the %s cutoff"
                % (NO_RECENT_INFERENCE, days_left(today, CUTOFF), CUTOFF))
    if days_since_ft_inference is None:
        return ("unknown-eligibility",
                "the inference clock could not be read, so eligibility is "
                "unknown rather than fine")
    if days_since_ft_inference == "none-in-window":
        return ("blocked-no-recent-inference",
                "no fine-tuned model produced a request anywhere in the window "
                "read, so the %d day rule has already closed creation. Read "
                "from usage, not from an attempt" % WINDOW)
    days = int(days_since_ft_inference)
    if days > WINDOW:
        return ("blocked-no-recent-inference",
                "no fine-tuned model has served a request for %d day(s), and "
                "the %d day rule has applied since %s, so new jobs are already "
                "being refused. Read from usage, not from an attempt"
                % (days, WINDOW, NO_RECENT_INFERENCE))
    if days > 45:
        return ("eligibility-expiring",
                "the last fine-tuned request was %d day(s) ago, so %d day(s) "
                "of the %d day window are left"
                % (days, WINDOW - days, WINDOW))
    return ("eligible",
            "the last fine-tuned request was %d day(s) ago and %d day(s) "
            "remain until the %s cutoff"
            % (days, days_left(today, CUTOFF), CUTOFF))


def family_for(base_model):
    """The deprecation family and replacement for a base. Pure. (family, to).

    Exact or hyphen-delimited, never a loose prefix. gpt-4.1-nano-2025-04-14
    starts with the characters gpt-4, and filing it under ft-gpt-4 would print
    the wrong replacement with complete confidence.
    """
    base = str(base_model or "")
    for prefix, family, replacement in FAMILIES:
        if base == prefix or base.startswith(prefix + "-"):
            return (family, replacement)
    return (None, None)


def serving_deadline(api_shutdown_date, family):
    """When this fine-tune stops serving. Pure. (date, source, detail).

    Returns where the date came from as well as the date. A field the API
    stated and a row in a published table are different grades of evidence and
    a reader is entitled to know which one they are looking at.
    """
    if api_shutdown_date:
        return (str(api_shutdown_date), "api",
                "shutdown_date read off the model object")
    if family:
        return (BASE_SHUTDOWN, "published-table",
                "the model object carried no shutdown_date, so this is the %s "
                "row in the deprecation table" % family)
    return (None, "unknown",
            "neither the model object nor the published table has a date for "
            "this base")


def job_verdict(status, fine_tuned_model, deadline, today):
    """Grade one job's serving half. Pure. Returns (state, detail)."""
    if str(status) != "succeeded" or not fine_tuned_model:
        return ("not-serving",
                "status %s with no fine-tuned model, so nothing is serving "
                "from this job" % status)
    if not deadline:
        return ("no-base-date",
                "no serving deadline could be established for this base")
    left = days_left(today, deadline)
    if left < 0:
        return ("already-dead",
                "the base shut down %d day(s) ago, so this fine-tune has "
                "stopped serving" % -left)
    if left <= 90:
        return ("dying-soon", "%d day(s) of inference left" % left)
    return ("serving", "%d day(s) of inference left" % left)


def repair_lines(state, replacement=None):
    """The repair for one verdict. Pure. Printed, never performed."""
    line = REPAIRS.get(state)
    if not line:
        return []
    if state in ("dying-soon", "already-dead") and replacement:
        return [line, "the documented replacement base is %s." % replacement]
    if state == "blocked-no-recent-inference":
        return [line,
                "note the order the dates fall in: the bases die %s and the "
                "right to retrain closes %s, so October is the deadline and "
                "January is only the outside edge."
                % (BASE_SHUTDOWN, CUTOFF)]
    return [line]


def get_json(session, path, key, params=None, timeout=30):
    """One GET. Returns (status, parsed body). Never raises on a 4xx."""
    try:
        r = session.get(API + path,
                        headers={"Authorization": "Bearer " + key},
                        params=params or {}, timeout=timeout)
    except requests.RequestException as exc:
        log.debug("GET %s failed: %s", path, exc)
        return (None, {})
    try:
        return (r.status_code, r.json())
    except ValueError:
        return (r.status_code, {})


def all_jobs(session, key, pages=20):
    """Walk GET /v1/fine_tuning/jobs to the end."""
    out, after = [], None
    for _ in range(pages):
        params = {"limit": 100}
        if after:
            params["after"] = after
        status, body = get_json(session, "/fine_tuning/jobs", key, params)
        if status != 200:
            log.warning("job listing came back %s, so eligibility cannot be "
                        "read from it", status)
            break
        page = body.get("data") or []
        out.extend(page)
        if not page or not body.get("has_more"):
            break
        after = page[-1].get("id")
        if not after:
            break
    return out


def days_since_ft_inference(session, key, today, days=70):
    """Days since any ft: model produced a request. Int, sentinel, or None."""
    start = int((dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=days)).timestamp())
    status, body = get_json(session, "/organization/usage/completions", key,
                            {"start_time": start, "bucket_width": "1d",
                             "group_by[]": ["model"], "limit": 180})
    if status != 200:
        log.warning("usage report came back %s, so the inference clock could "
                    "not be read", status)
        return None
    last = None
    for bucket in body.get("data") or []:
        stamp = bucket.get("start_time")
        if not stamp:
            continue
        day = dt.datetime.fromtimestamp(int(stamp),
                                        dt.timezone.utc).date().isoformat()
        for row in bucket.get("results") or []:
            model = str(row.get("model") or "")
            if model.startswith("ft:") and (row.get("num_model_requests") or 0) > 0:
                last = day if last is None else max(last, day)
    if last is None:
        return "none-in-window"
    return -days_left(today, last)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--today", default=dt.date.today().isoformat(),
                    help="override the date the arithmetic is done against")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project read key. This script only "
                  "issues GET requests and never submits a job")
        return 2

    session = requests.Session()
    findings = 0

    jobs = all_jobs(session, key)
    admin = os.environ.get("OPENAI_ADMIN_KEY")
    since = (days_since_ft_inference(session, admin, args.today)
             if admin else None)
    log.info("create: %d job(s) in the list, last ft: inference %s", len(jobs),
             "unknown" if since is None else
             "not in the window" if since == "none-in-window" else
             "%d day(s) ago" % since)

    state, detail = create_eligibility(args.today, bool(jobs), since)
    emit = log.warning if state in FINDINGS else log.info
    emit("%-28s %s", state, detail)
    for line in repair_lines(state):
        emit("  repair: %s", line)
    if state in FINDINGS:
        findings += 1

    succeeded = [j for j in jobs if str(j.get("status")) == "succeeded"
                 and j.get("fine_tuned_model")]
    log.info("serve: %d succeeded job(s)", len(succeeded))
    for job in succeeded:
        ftm = job.get("fine_tuned_model")
        base = job.get("model")
        family, replacement = family_for(base)
        _, ftm_body = get_json(session, "/models/" + str(ftm), key)
        shutdown = (ftm_body or {}).get("shutdown_date")
        if not shutdown and base:
            _, base_body = get_json(session, "/models/" + str(base), key)
            shutdown = (base_body or {}).get("shutdown_date")
        deadline, source, why = serving_deadline(shutdown, family)
        state, detail = job_verdict(job.get("status"), ftm, deadline,
                                    args.today)
        emit = log.warning if state in FINDINGS else log.info
        emit("  %-40s %-11s %-16s %-13s %s", ftm, deadline or "---", source,
             state, detail)
        log.debug("    %s", why)
        for line in repair_lines(state, replacement):
            emit("    repair: %s", line)
        if state in FINDINGS:
            findings += 1

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
