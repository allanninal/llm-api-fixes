"""Find OpenAI projects whose usage buckets went empty while the project is live.

Read only. GET requests against the organization endpoints, which reject
project keys: this needs an organization admin key (sk-admin-), and read-only
scopes are enough.

The finding is an absence. Nothing errored, because nothing was sent, so there
is no status code anywhere to look up. The usage endpoint returns buckets for
the whole window whether or not there was traffic, which makes the empty ones
readable, and the day axis is built from the window requested rather than from
the days that came back.

The repair is printed, never performed. What is missing is an alarm with a
floor instead of a ceiling, and that lives in your monitoring, not here.
"""
import argparse
import datetime as dt
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_project_went_quiet")

API = "https://api.openai.com/v1"

# Completions is one surface of eight, and a project can go quiet on one while
# staying busy on another. Quiet everywhere is a credential or a deploy; quiet
# on one is a code path, which is a far smaller thing to search.
SURFACES = ("completions", "embeddings", "images", "audio_speeches",
            "audio_transcriptions", "moderations", "file_search_calls",
            "web_search_calls")

# Each surface counts a different thing, and exactly one of these appears on any
# given result.
COUNT_FIELDS = ("num_model_requests", "num_requests", "num_images",
                "num_seconds", "num_characters")

FINDINGS = ("went-quiet",)


def _int(value):
    """Read a usage field as an int. Pure. Missing and unreadable both mean 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def day_key(epoch):
    """The UTC day a bucket start belongs to. Pure. None if unreadable."""
    try:
        return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def complete_days(now_epoch, days):
    """The last N complete UTC days, oldest first. Pure.

    Today is excluded. The current bucket is partial by definition and usage
    data lags, so a run of zeroes that includes today is one day shorter than
    it looks, and the whole finding is a run of zeroes.

    Built here rather than read off the response, because a project with no
    traffic may be absent from a bucket entirely and the missing days are the
    thing being looked for.
    """
    out = []
    for offset in range(int(days), 0, -1):
        key = day_key(int(now_epoch) - offset * 86400)
        if key is not None:
            out.append(key)
    return out


def daily(buckets):
    """{project_id: {day: count}} from one usage surface. Pure.

    Surfaces count different things, so the first recognised field wins rather
    than being summed: a result carrying both would otherwise be counted twice.
    """
    out = {}
    for bucket in buckets or []:
        day = day_key(bucket.get("start_time"))
        if day is None:
            continue
        for result in bucket.get("results") or []:
            project = str(result.get("project_id") or "unknown")
            count = 0
            for field in COUNT_FIELDS:
                if field in result:
                    count = _int(result.get(field))
                    break
            row = out.setdefault(project, {})
            row[day] = row.get(day, 0) + count
    return out


def classify(series, days, quiet_days=2, min_requests=100):
    """Classify one project's daily series. Pure. Returns (state, detail).

    Directional on purpose. Traffic in the early days and none in the last two
    is a project that stopped; the reverse is a project that started, and a
    check that cannot tell them apart fires on every launch and gets muted.
    """
    days = list(days or [])
    if len(days) <= quiet_days:
        return ("window-too-short",
                "%d complete day(s) is not enough to hold a %d day quiet "
                "window" % (len(days), quiet_days))

    series = series or {}
    head, tail = days[:-quiet_days], days[-quiet_days:]
    prior = sum(_int(series.get(day)) for day in head)
    recent = sum(_int(series.get(day)) for day in tail)
    active = [day for day in days if _int(series.get(day)) > 0]

    if not active:
        return ("never-active",
                "no traffic at all across %d complete day(s)" % len(days))
    if prior == 0:
        return ("new-traffic",
                "first traffic in this window landed on %s, inside the last %d "
                "day(s). A launch reads exactly like a death if you only "
                "compare halves." % (active[0], quiet_days))
    if recent > 0:
        return ("live",
                "%d request(s) in the last %d day(s), against a prior mean of "
                "%d a day" % (recent, quiet_days, prior / float(len(head))))
    if prior < min_requests:
        return ("too-little-traffic",
                "%d request(s) before the quiet window, under the floor of %d. "
                "Too sporadic for a gap to mean anything." % (prior, min_requests))

    since = len(days) - 1 - days.index(active[-1])
    return ("went-quiet",
            "last traffic on %s, %d complete day(s) ago, after a prior mean of "
            "%d request(s) a day"
            % (active[-1], since, prior / float(len(head))))


def key_activity(keys, now_epoch):
    """The newest last_used_at across a project's keys. Pure.

    Returns (epoch, days_since), or (None, None) when no key reports a use.
    """
    best = None
    for key in keys or []:
        try:
            used = key.get("last_used_at")
        except AttributeError:
            continue
        if used is None:
            continue
        try:
            used = int(used)
        except (TypeError, ValueError):
            continue
        if best is None or used > best:
            best = used
    if best is None:
        return (None, None)
    return (best, max(0.0, (int(now_epoch) - best) / 86400.0))


def corroborate(days_since, quiet_days=2):
    """Line the key roster up against the silence. Pure. Returns (state, detail).

    A key still in use while the buckets are empty is a much narrower fault
    than a key that went quiet at the same moment: something is authenticating
    and not inferring.
    """
    if days_since is None:
        return ("no-key-use",
                "no key on this project reports a last use, so there is "
                "nothing here to corroborate the silence with")
    if days_since <= quiet_days:
        return ("key-still-used",
                "a key on this project was used %.1f day(s) ago while the "
                "usage buckets were empty. Something is still authenticating "
                "and not inferring: a health check, or a surface this sweep "
                "did not read." % days_since)
    return ("key-quiet-too",
            "the newest key use is %.1f day(s) ago, which lines up with the "
            "buckets. The integration went quiet, not one call site."
            % days_since)


def surface_split(states):
    """(quiet, live) surface names for one project. Pure.

    Quiet on one surface while another is still busy is a code path rather than
    a credential, and that difference is worth more than the finding itself.
    """
    quiet = sorted(name for name, state in (states or {}).items()
                   if state == "went-quiet")
    live = sorted(name for name, state in (states or {}).items()
                  if state == "live")
    return (quiet, live)


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an "
                         "organization admin key (sk-admin-), not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params, max_pages=40):
    """Walk a usage report, which paginates on an opaque page cursor."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, path, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params = dict(params)
        params["page"] = page["next_page"]


def listing(session, path, params, max_pages=20):
    """Walk a list endpoint, which paginates on an object id."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, path, params)
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params = dict(params)
        params["after"] = data[-1].get("id")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14,
                    help="complete days to read (default 14)")
    ap.add_argument("--quiet-days", type=int, default=2,
                    help="days of silence that make a finding (default 2)")
    ap.add_argument("--min-requests", type=int, default=100,
                    help="ignore projects quieter than this before the gap "
                         "(default 100)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print projects that are still live")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key; read-only "
                  "scopes are enough)")
        return 2

    now = int(time.time())
    days = complete_days(now, max(3, min(int(args.days), 30)))
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + admin})

    projects = [p for p in listing(session, "/organization/projects", {"limit": 100})
                if str(p.get("status") or "") == "active"]
    if not projects:
        log.info("no active projects in this organization")
        return 0

    per_surface = {}
    for surface in SURFACES:
        try:
            per_surface[surface] = daily(pages(
                session, "/organization/usage/" + surface,
                {"start_time": now - (len(days) + 1) * 86400,
                 "bucket_width": "1d", "limit": len(days) + 1,
                 "group_by": ["project_id"]}))
        except requests.HTTPError:
            # A surface the organization has never used can 400 rather than
            # returning an empty window. Not a finding, and not fatal.
            log.info("skipped the %s usage surface", surface)

    checked = 0
    bad = 0
    for project in projects:
        project_id = str(project.get("id") or "")
        states = {}
        details = {}
        for surface, rows in per_surface.items():
            state, detail = classify(rows.get(project_id), days,
                                     args.quiet_days, args.min_requests)
            states[surface] = state
            details[surface] = detail
        checked += 1

        quiet, live = surface_split(states)
        if not quiet:
            if args.show_all:
                log.info("%-18s %s  no surface went quiet", "live", project_id)
            continue

        bad += 1
        log.warning("%-18s %s  %s: %s", "went-quiet", project_id, quiet[0],
                    details[quiet[0]])
        keys = list(listing(session,
                            "/organization/projects/%s/api_keys" % project_id,
                            {"limit": 100, "owner_project_access": "any"}))
        _, note = corroborate(key_activity(keys, now)[1], args.quiet_days)
        log.warning("  %s", note)
        if live:
            log.warning("  still live on: %s", ", ".join(live))
            log.warning("  repair: one code path stopped calling, not the "
                        "credential. Look at the deploy that touched it rather "
                        "than at the key.")
        else:
            log.warning("  repair: every surface is quiet, so look at the "
                        "credential, the feature flag or the consumer before "
                        "the call site.")
        log.warning("  repair: add a scheduled liveness check that alerts on "
                    "absence. Read /v1/organization/usage/completions daily "
                    "with group_by=project_id and page on next_page, and alert "
                    "when a project falls below a floor rather than above a "
                    "ceiling. This is the one check whose value is that it "
                    "fires on zero.")

    log.info("%d active project(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
