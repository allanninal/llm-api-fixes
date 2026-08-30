"""Trend retained vector store bytes against the queries that justify them.

Read only. Three paged GETs against /v1/organization/* with an admin key, plus
one optional GET of /v1/vector_stores with a project key for the per-store
snapshot. No request body is constructed and no file_search query is ever run.

Storage is a stock rather than a flow: it bills on bytes retained per unit of
time, so it does not fall when traffic does. The finding is therefore a slope
rather than a share, and it is only a finding when the slope is not matched by
query volume. Bytes growing alongside searches is a corpus doing its job.

One asymmetry shapes everything below. The vector stores usage endpoint groups
by project_id and nothing else, so there is no per-store byte series to ask
for; file search calls can be grouped by vector_store_id. Naming an individual
store therefore requires the current snapshot, which needs a project key.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_vector_store_storage_trend")

API = "https://api.openai.com/v1"
BETA = {"OpenAI-Beta": "assistants=v2"}

# Rows the report could not attribute to a project or a store. Kept under an
# explicit name and never folded into a real id, because a null that becomes a
# key is how one enormous fictional project gets reported.
UNGROUPED = "ungrouped"

# The unit that identifies storage on the cost report. Selecting on this rather
# than on a line item's display name is the difference between a check that
# survives a relabel and one that silently starts returning zero.
STORAGE_UNIT = "gibibyte_hours"

GIB = 1073741824.0
DAY = 86400

FINDINGS = ("bytes-growing-queries-flat", "bytes-growing-never-queried")


def byte_series(buckets):
    """{project_id: [(start_time, usage_bytes)]} sorted by time. Pure."""
    rows = {}
    for bucket in buckets or []:
        start = (bucket or {}).get("start_time")
        for result in (bucket or {}).get("results") or []:
            row = result or {}
            key = str(row.get("project_id") or UNGROUPED)
            try:
                value = int(row.get("usage_bytes") or 0)
            except (TypeError, ValueError):
                continue
            rows.setdefault(key, []).append((int(start or 0), value))
    for points in rows.values():
        points.sort()
    return rows


def query_series(buckets):
    """{project_id: [(start_time, num_requests)]} sorted by time. Pure."""
    rows = {}
    for bucket in buckets or []:
        start = (bucket or {}).get("start_time")
        for result in (bucket or {}).get("results") or []:
            row = result or {}
            key = str(row.get("project_id") or UNGROUPED)
            try:
                value = int(row.get("num_requests") or 0)
            except (TypeError, ValueError):
                continue
            rows.setdefault(key, []).append((int(start or 0), value))
    for points in rows.values():
        points.sort()
    return rows


def searches_by_store(buckets):
    """{vector_store_id: total num_requests}. Pure.

    The one per-store number available anywhere in the usage API. There is no
    matching per-store byte series: the vector stores endpoint groups by
    project_id only.
    """
    rows = {}
    for bucket in buckets or []:
        for result in (bucket or {}).get("results") or []:
            row = result or {}
            key = str(row.get("vector_store_id") or UNGROUPED)
            try:
                value = int(row.get("num_requests") or 0)
            except (TypeError, ValueError):
                continue
            rows[key] = rows.get(key, 0) + value
    return rows


def slope(points):
    """Least-squares trend in units per day. Pure. Zero on fewer than 2 points."""
    rows = sorted(points or [])
    if len(rows) < 2:
        return 0.0
    base = rows[0][0]
    xs = [(t - base) / float(DAY) for t, _ in rows]
    ys = [float(v) for _, v in rows]
    n = float(len(rows))
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def growth(points):
    """(first, last, delta, fraction) over a series. Pure.

    The fraction is delta over first, and is 0.0 rather than infinity when the
    series starts at zero, because "grew infinitely from nothing" is a division
    artefact rather than a reading anybody can act on.
    """
    rows = sorted(points or [])
    if not rows:
        return (0, 0, 0, 0.0)
    first = rows[0][1]
    last = rows[-1][1]
    delta = last - first
    fraction = (float(delta) / float(first)) if first > 0 else 0.0
    return (first, last, delta, fraction)


def storage_lines(buckets):
    """{line_item: {"dollars": x, "gibibyte_hours": q}} for storage only. Pure.

    Selected on quantity_unit, never on the line item's name.
    """
    rows = {}
    for bucket in buckets or []:
        for result in (bucket or {}).get("results") or []:
            row = result or {}
            if str(row.get("quantity_unit") or "") != STORAGE_UNIT:
                continue
            name = str(row.get("line_item") or "unlabelled")
            try:
                dollars = float((row.get("amount") or {}).get("value") or 0.0)
            except (TypeError, ValueError):
                dollars = 0.0
            try:
                quantity = float(row.get("quantity") or 0.0)
            except (TypeError, ValueError):
                quantity = 0.0
            entry = rows.setdefault(name, {"dollars": 0.0, STORAGE_UNIT: 0.0})
            entry["dollars"] += dollars
            entry[STORAGE_UNIT] += quantity
    return rows


def idle_stores(stores, searches, now, min_bytes=1073741824):
    """[(id, name, bytes, idle_days)] for stores nothing searched. Pure.

    The join the usage API cannot do for you: per-store query counts against a
    current snapshot. A store under the size floor is skipped, because a
    finding about 40 MiB is a finding about nothing.
    """
    out = []
    for store in stores or []:
        row = store or {}
        sid = str(row.get("id") or "")
        try:
            size = int(row.get("usage_bytes") or 0)
        except (TypeError, ValueError):
            continue
        if not sid or size < min_bytes:
            continue
        if int((searches or {}).get(sid, 0)) > 0:
            continue
        try:
            last = int(row.get("last_active_at") or 0)
        except (TypeError, ValueError):
            last = 0
        idle = int((now - last) / DAY) if last > 0 else -1
        out.append((sid, str(row.get("name") or "(unnamed)"), size, idle))
    out.sort(key=lambda r: (-r[2], r[0]))
    return out


def verdict(bytes_points, query_points, days, min_gib=1.0, min_growth=0.25):
    """Classify one project. Pure. Returns (state, detail).

    The absolute size floor comes before the growth rate, always. A project
    holding forty megabytes can triple its storage in a week and the reading is
    worth nothing.
    """
    first, last, _delta, fraction = growth(bytes_points)
    queries = sum(v for _, v in (query_points or []))

    if last < min_gib * GIB:
        return ("below-threshold",
                "%.1f GiB, under the %.1f GiB floor" % (last / GIB, min_gib))
    if fraction < min_growth:
        return ("flat",
                "%.1f GiB, %+.0f%% over %d day(s), %s file search call(s)"
                % (last / GIB, fraction * 100, days, format(queries, ",")))

    shape = ("%.1f GiB -> %.1f GiB (%+.0f%%)"
             % (first / GIB, last / GIB, fraction * 100))
    if queries <= 0:
        return ("bytes-growing-never-queried",
                "%s, 0 file search call(s) in %d day(s)" % (shape, days))
    if slope(query_points) <= 0:
        return ("bytes-growing-queries-flat",
                "%s while file search calls are flat or falling across the same "
                "window" % shape)
    return ("bytes-and-queries-growing",
            "%s, %s file search call(s). Growth, priced correctly."
            % (shape, format(queries, ",")))


def repair_lines(state, idle=()):
    """The repair for one verdict. Pure. Printed, never performed."""
    idle = list(idle or [])
    if state in FINDINGS:
        lines = []
        if state == "bytes-growing-never-queried":
            lines.append("no query has touched this project's stores in the "
                         "window. The bytes are being retained, not used.")
        else:
            lines.append("the corpus is growing and the query volume is not, "
                         "so you are paying more each month for the same "
                         "amount of retrieval.")
        if idle:
            lines.append("idle stores holding real bytes: " + "; ".join(
                "%s %s %.1f GiB%s" % (sid, name, size / GIB,
                                      "" if days < 0 else
                                      ", last active %d day(s) ago" % days)
                for sid, name, size, days in idle[:8]))
        else:
            lines.append("no per-store snapshot was read, so the project is "
                         "named and the store is not. Add a project key to "
                         "join the query counts against GET /v1/vector_stores.")
        lines.append("delete the dead ones with "
                     "DELETE /v1/vector_stores/{vector_store_id} after "
                     "archiving anything you still need.")
        lines.append("set an expiration policy at creation on stores that are "
                     "meant to be temporary, so the next prototype ages out on "
                     "its own rather than being somebody's future ticket.")
        return lines
    if state == "bytes-and-queries-growing":
        return ["nothing to do. This is a corpus that is being used more, and "
                "the storage line is supposed to follow it."]
    return []


def get(session, path, **params):
    r = session.get(API + path, params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/* needs an "
                         "organization admin key, not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def usage_buckets(session, path, params, max_pages=40):
    """Walk a usage report. limit caps at 31 daily buckets, so this pages."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, path, **params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def paged(session, path, max_pages=200, **params):
    """Walk an after/last_id cursor listing."""
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, path, **params)
        data = page.get("data") or []
        for item in data:
            yield item
        if not page.get("has_more") or not data:
            return
        params["after"] = page.get("last_id") or (data[-1] or {}).get("id")


def window_start(days, now=None):
    """Unix seconds at midnight UTC, `days` ago."""
    now = now or dt.datetime.now(dt.timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - dt.timedelta(days=days)).timestamp())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=90,
                    help="days of daily buckets to trend (default 90)")
    ap.add_argument("--min-gib", type=float, default=1.0,
                    help="size floor below which growth is not graded")
    ap.add_argument("--min-growth", type=float, default=0.25,
                    help="fractional growth above which a slope is a finding")
    args = ap.parse_args()

    admin = os.environ.get("OPENAI_ADMIN_KEY")
    if not admin:
        log.error("set OPENAI_ADMIN_KEY to an organization admin key; a "
                  "project key cannot read /v1/organization/*")
        return 2

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + admin})

    start = window_start(args.days)
    common = {"start_time": start, "bucket_width": "1d", "limit": 31}

    bytes_buckets = list(usage_buckets(
        s, "/organization/usage/vector_stores",
        dict(common, group_by="project_id")))
    search_buckets = list(usage_buckets(
        s, "/organization/usage/file_search_calls",
        dict(common, group_by=["project_id", "vector_store_id"])))
    cost_buckets = list(usage_buckets(
        s, "/organization/costs", dict(common, group_by="line_item")))

    by_project = byte_series(bytes_buckets)
    queries = query_series(search_buckets)
    per_store = searches_by_store(search_buckets)

    stores = []
    project_key = os.environ.get("OPENAI_API_KEY")
    if project_key:
        p = requests.Session()
        p.headers.update({"Authorization": "Bearer " + project_key, **BETA})
        stores = list(paged(p, "/vector_stores", limit=100))

    log.info("%d day(s) of daily buckets across %d project(s), %d store(s) in "
             "the snapshot", args.days, len(by_project), len(stores))

    lines = storage_lines(cost_buckets)
    dollars = sum(v["dollars"] for v in lines.values())
    hours = sum(v[STORAGE_UNIT] for v in lines.values())
    if lines:
        log.info("storage cost in the window: $%s over %s %s",
                 format(round(dollars, 2), ",.2f"), format(round(hours, 1), ","),
                 STORAGE_UNIT)
    else:
        log.info("no cost result carried quantity_unit %r in the window, so "
                 "nothing is being billed for storage yet", STORAGE_UNIT)

    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    idle = idle_stores(stores, per_store, now,
                       min_bytes=int(args.min_gib * GIB))

    findings = 0
    for project in sorted(by_project):
        state, detail = verdict(by_project[project], queries.get(project, []),
                                args.days, args.min_gib, args.min_growth)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-27s %s: %s", state, project, detail)
        for line in repair_lines(state, idle if state in FINDINGS else ()):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    if per_store.get(UNGROUPED):
        log.info("%s file search call(s) came back with no vector_store_id and "
                 "are not attributed to a store",
                 format(per_store[UNGROUPED], ","))

    log.info("%d finding(s)", findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
