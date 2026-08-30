"""Find fine-tuning jobs that were accepted, then failed, and never read.

Read only. GET /v1/fine_tuning/jobs, paginated, plus GET on the events feed for
jobs that failed. Nothing is created, cancelled, deleted or re-uploaded.

Job creation is asynchronous. The create call returns 200 as soon as the job is
accepted, and validation and training failures surface only on the job object:
status becomes failed, fine_tuned_model and trained_tokens stay null, and error
carries code, message and param. None of that is pushed anywhere.

The error codes are an open set. The documented ones are translated into an
action; everything else is printed exactly as returned, because inventing an
interpretation sends somebody confidently in a direction the API never suggested.

Scope: this note owns a job that failed. Whether a job that succeeded is ever
called is fine-tuned-model-never-used, and whether new jobs can be created at
all is a platform question about dates. Neither is read here.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_fine_tune_failures")

JOBS_URL = "https://api.openai.com/v1/fine_tuning/jobs"

TERMINAL = ("succeeded", "failed", "cancelled")

# Only the codes with a documented meaning. Anything else is printed verbatim.
ADVICE = {
    "invalid_training_file":
        "the JSONL is malformed. One JSON object per line, no trailing blank "
        "line, no BOM, each row a messages array with at least one assistant "
        "turn, and one schema across every row.",
    "invalid_validation_file":
        "the validation file has the same problem as a malformed training "
        "file, and error.param says which of the two was rejected.",
    "invalid_n_examples":
        "the example count is out of range: too few rows to train on, or more "
        "than the method accepts. Count the lines before uploading.",
    "exceeded_quota":
        "this is a billing problem rather than a data one. Editing the file "
        "will not help; check the account's quota and spend limits.",
}

FINDINGS = ("job-failed", "failed-without-error", "stalled-in-validation")


def job_row(body):
    """One job, reduced. Pure. The error object is flattened.

    Flattened deliberately: a job with no error key and a job with an empty
    error object mean the same thing to a reader and should not need two code
    paths to say so.
    """
    body = body if isinstance(body, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    try:
        created = int(body.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0
    return {"id": str(body.get("id") or ""),
            "status": str(body.get("status") or ""),
            "model": str(body.get("model") or ""),
            "fine_tuned_model": str(body.get("fine_tuned_model") or ""),
            "created_at": created,
            "code": str((error or {}).get("code") or ""),
            "param": str((error or {}).get("param") or ""),
            "message": str((error or {}).get("message") or "")}


def hours_since(created_at, now):
    """Age in hours. Pure. The clock is an argument."""
    try:
        created = int(created_at)
        now = int(now)
    except (TypeError, ValueError):
        return None
    if created <= 0:
        return None
    return (now - created) / 3600.0


def error_advice(code):
    """The documented meaning of one code. Pure. Empty for anything else."""
    return ADVICE.get(str(code or "").strip(), "")


def error_events(events):
    """Error-level messages in order. Pure. De-duplicated, never reordered."""
    out = []
    for item in events or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("level") or "").lower() != "error":
            continue
        message = str(item.get("message") or "").strip()
        if message and message not in out:
            out.append(message)
    return out


def classify_job(row, now, stall_hours):
    """Grade one job. Pure. Returns (state, detail)."""
    row = row or {}
    status = str(row.get("status") or "")
    job_id = row.get("id") or "(no id)"
    if status == "failed" and row.get("code"):
        return ("job-failed",
                "%s: failed on %s with %s"
                % (job_id, row.get("param") or "an unnamed input",
                   row.get("code")))
    if status == "failed":
        return ("failed-without-error",
                "%s: failed with no error code on the job object, so the events "
                "feed is the only account of why" % job_id)
    if status == "validating_files":
        age = hours_since(row.get("created_at"), now)
        if age is not None and age >= stall_hours:
            return ("stalled-in-validation",
                    "%s: %.1f hours in validating_files, which is not progress"
                    % (job_id, age))
        return ("validating", "%s: validating files" % job_id)
    if status == "succeeded":
        return ("succeeded",
                "%s: succeeded, which is a different note" % job_id)
    if status == "cancelled":
        return ("cancelled", "%s: cancelled by somebody on purpose" % job_id)
    if status in ("queued", "running"):
        return ("running", "%s: %s" % (job_id, status))
    return ("unknown-status",
            "%s: status %r is not one this script recognises"
            % (job_id, status or "(none)"))


def repair_lines(state, code=""):
    """The repair for one verdict. Pure. Printed, never performed."""
    poll = ("poll the job to a terminal status in CI and fail the build on "
            "anything that is not succeeded. A 200 on create is a receipt, not "
            "a result.")
    if state == "job-failed":
        advice = error_advice(code)
        if advice:
            return [advice, poll]
        return ["the code %r is not one this script has a documented meaning "
                "for. Read error.message and the events feed above as printed, "
                "and do not act on a guess." % (code or "(none)"), poll]
    if state == "failed-without-error":
        return ["read GET /v1/fine_tuning/jobs/{id}/events for this job. The "
                "terminal status is all the job object recorded.", poll]
    if state == "stalled-in-validation":
        return ["read the events feed for the line that validation stopped on, "
                "and delete the file if it is a dead upload still counting "
                "against project storage.", poll]
    if state == "succeeded":
        return []
    return []


def fetch_jobs(key, timeout=30):
    """Paged GET of the job list. Returns (rows, error)."""
    rows = []
    params = {"limit": 100}
    headers = {"Authorization": "Bearer " + key}
    for _ in range(100):
        try:
            r = requests.get(JOBS_URL, headers=headers, params=params,
                             timeout=timeout)
        except requests.RequestException as exc:
            return (rows, "request failed: %s" % exc)
        if r.status_code != 200:
            return (rows, "HTTP %d %s" % (r.status_code, (r.text or "")[:160]))
        body = r.json()
        data = body.get("data") or []
        rows.extend(job_row(item) for item in data)
        if not body.get("has_more") or not data:
            break
        params["after"] = data[-1].get("id")
    return (rows, None)


def fetch_events(job_id, key, timeout=30):
    """GET the events feed for one job. Returns a list, empty on any problem."""
    try:
        r = requests.get("%s/%s/events" % (JOBS_URL, job_id),
                         headers={"Authorization": "Bearer " + key},
                         params={"limit": 100}, timeout=timeout)
    except requests.RequestException as exc:
        log.debug("events for %s failed: %s", job_id, exc)
        return []
    if r.status_code != 200:
        return []
    try:
        return list(reversed(r.json().get("data") or []))
    except ValueError:
        return []


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stall-hours", type=float, default=2.0,
                    help="hours in validating_files that count as stalled")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key set to Read Only. Both "
                  "calls are GETs of /v1/fine_tuning/jobs")
        return 2

    rows, err = fetch_jobs(key)
    if err:
        log.error("%s", err)
        return 2
    if not rows:
        log.info("no fine-tuning jobs in this project, so there is nothing to "
                 "grade")
        return 0

    now = int(time.time())
    findings = 0
    for row in sorted(rows, key=lambda r: -int(r.get("created_at") or 0)):
        state, detail = classify_job(row, now, args.stall_hours)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-10s %-16s base %-16s created %s", row["id"], row["status"],
             row["model"] or "(none)",
             time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(row["created_at"] or 0)))
        if row.get("code"):
            emit("  error.code    %s", row["code"])
        if row.get("param"):
            emit("  error.param   %s", row["param"])
        if row.get("message"):
            emit("  error.message %s", row["message"])
        if state in ("job-failed", "failed-without-error",
                     "stalled-in-validation"):
            for message in error_events(fetch_events(row["id"], key))[:5]:
                emit("  event         %s", message)
        emit("%-21s %s", state, detail)
        for line in repair_lines(state, row.get("code")):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    log.info("%d job(s), %d finding(s)", len(rows), findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
