"""Report OpenAI fine-tuned models that were trained, billed, and never called.

Read only. GET requests and nothing else, and it needs two credentials because
no single key can answer the question:

  OPENAI_API_KEY    a project key set to Read Only, for /v1/fine_tuning/jobs,
                    /v1/models and /v1/files
  OPENAI_ADMIN_KEY  an organization admin key with read scopes, for
                    /v1/organization/usage/completions

The repair is printed, never performed. Deleting a custom model somebody spent
a quarter producing is a decision with an owner, and that owner is not a cron.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_fine_tune_usage_audit")

API = "https://api.openai.com/v1"

# Published platform dates. Fine-tuned snapshots built on a retired base model
# stop answering on the first; new fine-tuning jobs cannot be created after the
# second. Both are printed rather than acted on.
BASE_RETIREMENT = "2026-10-23"
NEW_JOBS_BLOCKED = "2027-01-06"

FINDINGS = ("never-called", "never-called-base-gone", "in-service-base-gone")


def base_model(fine_tuned_model):
    """The base model a fine-tune id was built on, or None. Pure.

    "ft:gpt-4o-mini-2024-07-18:acme::AbC123" -> "gpt-4o-mini-2024-07-18". The
    optional suffix segment moves the trailing id along, so this reads the
    second field rather than counting from the end.
    """
    name = str(fine_tuned_model or "").strip()
    if not name.lower().startswith("ft:"):
        return None
    parts = name.split(":")
    if len(parts) < 3 or not parts[1]:
        return None
    return parts[1]


def days_until(date_str, now):
    """Whole days from now until an ISO date, or None if unreadable. Pure.

    Negative once the date has passed. Floored toward the past, so a deadline
    fourteen hours away reads as 0 days rather than 1: this number is printed to
    somebody who will act on it tomorrow.
    """
    try:
        year, month, day = (int(p) for p in str(date_str).split("-"))
        target = dt.datetime(year, month, day, tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None
    return int((target - now).total_seconds() // 86400)


def verdict(job, requests_made, available_models, now, window_days=30):
    """Classify one fine-tuning job against its usage. Pure. Returns (state, detail).

    available_models is the set of ids GET /v1/models returned, which is what the
    key can actually call. A base missing from it puts a deadline on the custom
    model whether or not anyone is using it, so that case is split out rather
    than folded into the idle one.
    """
    status = str(job.get("status") or "").strip().lower()
    if status != "succeeded":
        return ("not-succeeded",
                "status is %s, so there is no model id to look for usage against"
                % (status or "missing"))

    model_id = str(job.get("fine_tuned_model") or "").strip()
    if not model_id:
        return ("unnamed",
                "the job succeeded and carries no fine_tuned_model. Read the "
                "object by hand rather than assuming nothing was produced.")

    try:
        trained = int(job.get("trained_tokens") or 0)
    except (TypeError, ValueError):
        trained = 0
    try:
        calls = int(requests_made or 0)
    except (TypeError, ValueError):
        calls = 0

    base = job.get("model") or base_model(model_id)
    base_gone = bool(base) and base not in set(available_models or ())
    deadline = days_until(BASE_RETIREMENT, now)
    clock = ("" if deadline is None else
             " Fine-tunes on retired base models stop answering in %d day(s)."
             % deadline)

    if calls > 0:
        if base_gone:
            return ("in-service-base-gone",
                    "%d request(s) in %d days, but the base model %s is no "
                    "longer listed by GET /v1/models. This fine-tune is serving "
                    "traffic and is going to stop.%s"
                    % (calls, window_days, base, clock))
        return ("in-service",
                "%d request(s) in %d days" % (calls, window_days))

    if base_gone:
        return ("never-called-base-gone",
                "0 request(s) in %d days, %d trained token(s), and the base "
                "model %s is no longer listed. Nothing to migrate and nothing "
                "to lose.%s" % (window_days, trained, base, clock))

    return ("never-called",
            "0 request(s) in %d days, %d trained token(s). Training was billed "
            "and inference never happened." % (window_days, trained))


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI on %s: wrong key for this endpoint. "
                         "Jobs, models and files want the project key; usage "
                         "wants the admin key." % path)
    r.raise_for_status()
    return r.json()


def jobs(session, max_pages=20):
    """Walk GET /v1/fine_tuning/jobs, which paginates on the last job's id."""
    params = {"limit": 100}
    for _ in range(max_pages):
        page = get(session, "/fine_tuning/jobs", params)
        data = page.get("data") or []
        for job in data:
            yield job
        if not page.get("has_more") or not data:
            return
        params = {"limit": 100, "after": data[-1].get("id")}


def requests_by_model(session, start_time, days, max_pages=20):
    """Summed num_model_requests per model id. Needs the admin key."""
    out = {}
    params = {"start_time": start_time, "bucket_width": "1d", "limit": days,
              "group_by": "model"}
    for _ in range(max_pages):
        page = get(session, "/organization/usage/completions", params)
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                model = str(result.get("model") or "")
                if not model:
                    continue
                try:
                    out[model] = out.get(model, 0) + int(
                        result.get("num_model_requests") or 0)
                except (TypeError, ValueError):
                    pass
        cursor = page.get("next_page")
        if not cursor:
            return out
        params = dict(params, page=cursor)
    return out


def available_model_ids(session):
    page = get(session, "/models")
    return {str(m.get("id")) for m in page.get("data") or [] if m.get("id")}


def result_file_bytes(session):
    """Total bytes still held by fine-tune result files, and how many there are."""
    page = get(session, "/files", {"purpose": "fine-tune-results", "limit": 100})
    files = page.get("data") or []
    total = 0
    for f in files:
        try:
            total += int(f.get("bytes") or 0)
        except (TypeError, ValueError):
            pass
    return len(files), total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="usage window in days (default 30)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print jobs that are in service or not succeeded")
    args = ap.parse_args()

    project_key = os.environ.get("OPENAI_API_KEY")
    admin_key = os.environ.get("OPENAI_ADMIN_KEY")
    if not project_key or not admin_key:
        log.error("set OPENAI_API_KEY (a project key set to Read Only) and "
                  "OPENAI_ADMIN_KEY (an organization admin key with read scopes)")
        return 2

    project = requests.Session()
    project.headers.update({"Authorization": "Bearer " + project_key})
    admin = requests.Session()
    admin.headers.update({"Authorization": "Bearer " + admin_key})

    now = dt.datetime.now(dt.timezone.utc)
    start = int((now - dt.timedelta(days=args.days)).timestamp())

    usage = requests_by_model(admin, start, args.days)
    available = available_model_ids(project)

    checked = 0
    bad = 0
    for job in jobs(project):
        model_id = str(job.get("fine_tuned_model") or "")
        state, detail = verdict(job, usage.get(model_id, 0), available, now,
                                args.days)
        if state != "not-succeeded":
            checked += 1
        line = "%-22s %-42s %s" % (state, model_id or job.get("id"), detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            checkpoints = get(project, "/fine_tuning/jobs/%s/checkpoints"
                              % job.get("id")).get("data") or []
            for cp in checkpoints:
                cp_id = cp.get("fine_tuned_model_checkpoint")
                if cp_id:
                    log.warning("  checkpoint %s: %d request(s) in the window",
                                cp_id, usage.get(str(cp_id), 0))
            log.warning("  repair: route traffic to it or retire it. Deleting "
                        "the custom model and its result_files stops the "
                        "storage charge; GET /v1/files?purpose=fine-tune-results "
                        "lists them.")
            left = days_until(NEW_JOBS_BLOCKED, now)
            if left is not None:
                log.warning("  repair: decide before the platform decides. New "
                            "fine-tuning jobs cannot be created after %s, %d "
                            "day(s) away.", NEW_JOBS_BLOCKED, left)
        elif args.show_all:
            log.info(line)

    count, total_bytes = result_file_bytes(project)
    if count:
        log.info("%d fine-tune result file(s) still stored, %.1f MB",
                 count, total_bytes / 1048576.0)

    log.info("%d succeeded job(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
