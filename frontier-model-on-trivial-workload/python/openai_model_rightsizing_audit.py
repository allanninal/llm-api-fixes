"""Report OpenAI models that are larger than the work they are doing.

Read only. GET requests and nothing else: OPENAI_ADMIN_KEY must be an
organization admin key (sk-admin-...) with read scopes, because every
/v1/organization endpoint rejects a project key outright.

The repair is printed, never performed. Which model serves production traffic
is a deploy, and restricting a project's model permissions changes what your
colleagues are allowed to call. Neither belongs to an audit script.
"""
import argparse
import datetime as dt
import logging
import os
import re
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_model_rightsizing_audit")

API = "https://api.openai.com/v1"

# Substrings that mean "this is already the small sibling". Matched on the model
# id because there is no field on the usage result that says how big a model is.
SMALL_MARKERS = ("mini", "nano", "small", "lite", "embedding", "moderation")

# The families worth right-sizing, in the order they are tested. Each maps to the
# cheaper sibling that answers the same shape of question. Kept as a table rather
# than a string rule because "gpt-5" -> "gpt-5-mini" is a naming convention, not
# a guarantee, and a wrong suggestion here is worse than none.
SIBLINGS = (
    ("gpt-5", "gpt-5-mini"),
    ("gpt-4.1", "gpt-4.1-mini"),
    ("gpt-4o", "gpt-4o-mini"),
    ("o3", "o4-mini"),
    ("o1", "o4-mini"),
)

FINDINGS = ("oversized",)


def tier(model):
    """Classify a model id. Pure, and deliberately conservative.

    Returns "custom" for a fine-tune, "small" for a model that is already the
    cheap sibling, "premium" for a family with a cheaper sibling to move to, and
    "unknown" for everything else. Unknown is not a finding: a model this table
    has never heard of is a model this script has no business advising on.
    """
    name = str(model or "").strip().lower()
    if not name:
        return "unknown"
    if name.startswith("ft:"):
        return "custom"
    if any(marker in name for marker in SMALL_MARKERS):
        return "small"
    for family, _cheaper in SIBLINGS:
        if name.startswith(family):
            return "premium"
    return "unknown"


def sibling(model):
    """The cheaper model that answers the same shape of question, or None. Pure."""
    name = str(model or "").strip().lower()
    if tier(name) != "premium":
        return None
    for family, cheaper in SIBLINGS:
        if name.startswith(family):
            return cheaper
    return None


def fold(pages):
    """Sum the daily buckets into one row per model. Pure.

    Folding before dividing matters: a mean taken per bucket and then averaged
    weights a quiet Sunday exactly as heavily as a Tuesday, which is how a model
    that is busy on weekdays acquires a flattering output-per-request number.

    project_ids are collected as a sorted list so the caller knows which projects
    to ask about model permissions, and so two runs print the same order.
    """
    out = {}
    for page in pages:
        for bucket in page.get("data") or []:
            for result in bucket.get("results") or []:
                model = str(result.get("model") or "").strip()
                if not model:
                    continue
                row = out.setdefault(model, {"requests": 0, "input": 0,
                                             "output": 0, "projects": set()})
                for field, key in (("num_model_requests", "requests"),
                                   ("input_tokens", "input"),
                                   ("output_tokens", "output")):
                    try:
                        row[key] += int(result.get(field) or 0)
                    except (TypeError, ValueError):
                        pass
                project = result.get("project_id")
                if project:
                    row["projects"].add(str(project))
    return {m: {**row, "projects": sorted(row["projects"])} for m, row in out.items()}


def verdict(model, row, min_requests=500, trivial_output=50, long_input=20000):
    """Classify one folded model row. Pure. Returns (state, detail).

    The order is the argument. A model with too few calls has no shape to read.
    A model that is already small, or that this script does not recognise, is
    not advised on at all. Only then does the ratio decide, and short answers
    over enormous prompts are separated out because the money there is on the
    input side and swapping the model saves almost none of it.
    """
    try:
        requests_made = int(row.get("requests") or 0)
    except (TypeError, ValueError):
        return ("unreadable",
                "num_model_requests did not sum to an integer, so there is no "
                "denominator and no ratio to read")
    if requests_made <= 0:
        return ("unreadable",
                "0 request(s) in the window, so there is nothing to divide by")
    if requests_made < min_requests:
        return ("low-volume",
                "%d request(s) in the window, under the floor of %d. A mean "
                "taken over this few calls is noise, not a shape."
                % (requests_made, min_requests))

    out_per = (row.get("output") or 0) / float(requests_made)
    in_per = (row.get("input") or 0) / float(requests_made)
    shape = ("%d request(s), mean output %.0f token(s), mean input %.0f token(s)"
             % (requests_made, out_per, in_per))

    kind = tier(model)
    if kind == "custom":
        return ("custom-model",
                "%s. This is a fine-tune, and its size is inherited from the "
                "base model rather than chosen here." % shape)
    if kind == "small":
        return ("right-sized",
                "%s. Already the cheap sibling for its family." % shape)
    if kind != "premium":
        return ("unknown-model",
                "%s. No cheaper sibling is known for this model id, so this "
                "script has no recommendation to make about it." % shape)

    if out_per >= trivial_output:
        return ("deliberative",
                "%s. The answers are long enough that the model is plausibly "
                "doing the work it was chosen for." % shape)
    if in_per >= long_input:
        return ("input-bound",
                "%s. Short answers over very large prompts. The bill here is "
                "input, not model tier, so caching the prefix will save more "
                "than downgrading the model." % shape)
    return ("oversized",
            "%s. A premium model returning answers this short is answering "
            "questions a cheaper sibling would answer identically." % shape)


def permissions_state(perms, model):
    """Can this project still reach this model? Pure. Returns a state string.

    GET /v1/organization/projects/{id}/model_permissions returns a mode of
    allow_list or deny_list with a model_ids array. An unconstrained project is
    the durable half of the finding: without a restriction the expensive model
    comes back the next time somebody copies a snippet from the quickstart.
    """
    if not isinstance(perms, dict):
        return "unreadable"
    mode = str(perms.get("mode") or "").strip().lower()
    ids = perms.get("model_ids")
    if not isinstance(ids, list):
        ids = []
    ids = [str(i).strip().lower() for i in ids]
    name = str(model or "").strip().lower()

    if mode == "allow_list":
        if not ids:
            return "blocked"
        return "allowed" if name in ids else "blocked"
    if mode == "deny_list":
        if not ids:
            return "unconstrained"
        return "blocked" if name in ids else "allowed"
    return "unreadable"


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI: OPENAI_ADMIN_KEY must be an "
                         "organization admin key, not a project key")
    if r.status_code == 403:
        raise SystemExit("403 from OpenAI: the key is not authorised for "
                         "/v1/organization. A project key cannot read usage.")
    r.raise_for_status()
    return r.json()


def usage_pages(session, start_time, days, max_pages=20):
    """Walk the usage endpoint, which paginates on next_page."""
    params = {"start_time": start_time, "bucket_width": "1d", "limit": days,
              "group_by": ["model", "project_id"]}
    for _ in range(max_pages):
        page = get(session, "/organization/usage/completions", params)
        yield page
        cursor = page.get("next_page")
        if not cursor:
            return
        params = dict(params, page=cursor)


def spend_by_line_item(session, start_time):
    """Thirty days of spend, keyed by the cost report's line_item string."""
    out = {}
    page = get(session, "/organization/costs",
               {"start_time": start_time, "limit": 31, "group_by": "line_item"})
    for bucket in page.get("data") or []:
        for result in bucket.get("results") or []:
            item = str(result.get("line_item") or "")
            amount = (result.get("amount") or {}).get("value") or 0
            try:
                out[item] = out.get(item, 0.0) + float(amount)
            except (TypeError, ValueError):
                pass
    return out


def spend_for(model, spend):
    """Spend on exactly this model, from the cost report's line items. Pure.

    Substring matching is not good enough here. "gpt-5" occurs inside
    "gpt-5-mini, input tokens" and inside a fine-tune id built on it, and
    quoting either as the premium model's spend overstates the saving in the
    one line a reader is actually going to act on. So the match has to sit
    between boundaries: no letter, digit, dot, dash or colon on either side.
    """
    name = str(model or "").strip().lower()
    if not name:
        return 0.0
    pattern = re.compile(r"(?<![-a-z0-9.:])" + re.escape(name) + r"(?![-a-z0-9.])")
    total = 0.0
    for item, amount in (spend or {}).items():
        if pattern.search(str(item).lower()):
            try:
                total += float(amount)
            except (TypeError, ValueError):
                pass
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14,
                    help="days of usage to fold (default 14)")
    ap.add_argument("--min-requests", type=int, default=500,
                    help="ignore models with fewer calls than this (default 500)")
    ap.add_argument("--trivial-output", type=int, default=50,
                    help="mean output tokens under which work is trivial (default 50)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print models that are the right size")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_ADMIN_KEY")
    if not key:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key with read scopes)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    now = dt.datetime.now(dt.timezone.utc)
    usage_start = int((now - dt.timedelta(days=args.days)).timestamp())
    cost_start = int((now - dt.timedelta(days=30)).timestamp())

    rows = fold(usage_pages(session, usage_start, args.days))
    spend = spend_by_line_item(session, cost_start)

    checked = 0
    bad = 0
    for model in sorted(rows):
        row = rows[model]
        state, detail = verdict(model, row, args.min_requests, args.trivial_output)
        checked += 1
        line = "%-14s %-16s %s" % (state, model, detail)

        if state in FINDINGS:
            bad += 1
            log.warning(line)
            cheaper = sibling(model)
            money = spend_for(model, spend)
            log.warning("  repair: %s answers this shape of question; 30d spend "
                        "on %s was $%.2f", cheaper, model, money)
            for project in row["projects"]:
                perms = get(session,
                            "/organization/projects/%s/model_permissions" % project)
                where = permissions_state(perms, model)
                if where == "unconstrained":
                    log.warning("  repair: project %s is unconstrained. To make "
                                "the change durable, set model_permissions to "
                                "mode allow_list with model_ids [%r] so the "
                                "expensive model cannot come back.",
                                project, cheaper)
                else:
                    log.warning("  note: project %s model_permissions say %s",
                                project, where)
        elif state == "input-bound":
            log.warning(line)
            log.warning("  repair: read the prompt-caching note before changing "
                        "the model. A stable prefix at this size is the bill.")
        elif state in ("unreadable",):
            log.warning(line)
        elif args.show_all:
            log.info(line)

    log.info("%d model(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
