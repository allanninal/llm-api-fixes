"""Rank OpenAI organization spend by line item and by project.

Read only. Two GET requests against /v1/organization/costs, which rejects
project keys: this needs an organization admin key (sk-admin-), which can and
should be provisioned read-only.

Nothing here is broken. The finding is where the money is, which the default
ungrouped response cannot tell you, and the repair is a substitution or a
boundary printed for you to decide on.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_cost_concentration_audit")

API = "https://api.openai.com/v1"

# group_by on the costs endpoint takes only these. Not model: the model name
# lives inside the line_item string, next to the token side.
AXES = ("line_item", "project_id")

# quantity_unit is a small enumeration and only two members of it are tokens.
# The others are seconds, hours, gibibyte-hours, images and characters, and
# dividing dollars by those does not produce a price per million tokens.
TOKENS_PER_UNIT = {"tokens": 1.0, "1000_tokens": 1000.0}

FINDINGS = ("dominant", "top-heavy", "unattributable")


def rank(buckets, field):
    """Aggregate a grouped cost report by one field. Pure.

    Returns rows sorted by dollars descending, each carrying its share of the
    total. A row whose name is null keeps a null name: turning it into the
    string "unknown" would hide that the report answered precisely, and that
    the answer was "this spend belongs to no project".
    """
    rows = {}
    for bucket in buckets or []:
        for result in bucket.get("results") or []:
            raw = result.get(field)
            name = raw.strip() if isinstance(raw, str) and raw.strip() else None
            row = rows.setdefault(name, {"name": name, "amount": 0.0,
                                         "quantity": 0.0, "unit": None})
            try:
                row["amount"] += float((result.get("amount") or {}).get("value") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                row["quantity"] += float(result.get("quantity") or 0.0)
            except (TypeError, ValueError):
                pass
            unit = result.get("quantity_unit")
            unit = str(unit).strip() if isinstance(unit, str) and unit.strip() else None
            if unit and row["unit"] is None:
                row["unit"] = unit
            elif unit and row["unit"] not in (unit, "mixed"):
                row["unit"] = "mixed"

    total = sum(row["amount"] for row in rows.values())
    out = []
    for row in rows.values():
        row = dict(row)
        row["amount"] = round(row["amount"], 2)
        row["share"] = round(row["amount"] / total, 4) if total > 0 else 0.0
        out.append(row)
    out.sort(key=lambda r: (-r["amount"], r["name"] or ""))
    return out


def unit_price(amount, quantity, unit):
    """Dollars per million tokens for one row, or None. Pure.

    None for every unit that is not tokens. A row billed in images or in
    gibibyte-hours has a perfectly good unit price and it is not a token price,
    so reporting one would be inventing a number that looks comparable to the
    rows around it and is not.
    """
    scale = TOKENS_PER_UNIT.get(str(unit or "").strip().lower())
    if scale is None:
        return None
    try:
        tokens = float(quantity or 0.0) * scale
        dollars = float(amount or 0.0)
    except (TypeError, ValueError):
        return None
    if tokens <= 0:
        return None
    return round(dollars / (tokens / 1000000.0), 4)


def verdict(ranked, threshold=0.50, pair_threshold=0.75, min_spend=1.0):
    """Classify one axis of a ranking. Pure. Returns (state, detail).

    "spread" is an answer, not a failure to find something: a bill with no row
    above half is a bill with no single lever in it, and knowing that is worth
    the call. "unattributable" is kept separate from "dominant" because a
    largest row the report could not name is an attribution problem, and no
    amount of model substitution fixes it.
    """
    rows = [dict(row) for row in (ranked or [])]
    total = round(sum(float(row.get("amount") or 0.0) for row in rows), 2)
    if not rows or total < min_spend:
        return ("no-spend",
                "$%.2f across %d row(s), too little to rank" % (total, len(rows)))

    top = rows[0]
    share = float(top.get("amount") or 0.0) / total
    name = top.get("name")

    if name is None and share >= threshold:
        return ("unattributable",
                "%.0f%% of $%.2f is on a row the report returned with no name. "
                "Null is not unknown here: this axis cannot attribute that "
                "spend, which is a problem to fix before the cost is one to "
                "argue about." % (share * 100, total))

    if share >= threshold:
        return ("dominant",
                "%r is %.0f%% of $%.2f. Optimising anything else moves at most "
                "%.0f%% of the bill." % (name, share * 100, total,
                                         (1 - share) * 100))

    if len(rows) > 1:
        second = float(rows[1].get("amount") or 0.0) / total
        if share + second >= pair_threshold:
            return ("top-heavy",
                    "%r and %r are %.0f%% of $%.2f between them, with neither "
                    "above %.0f%% alone." % (name, rows[1].get("name"),
                                             (share + second) * 100, total,
                                             threshold * 100))

    return ("spread",
            "no single row above %.0f%% of $%.2f across %d row(s)"
            % (threshold * 100, total, len(rows)))


def get(session, params):
    r = session.get(API + "/organization/costs", params=params, timeout=90)
    if r.status_code in (401, 403):
        raise SystemExit("%d from OpenAI: /v1/organization/costs needs an "
                         "organization admin key (sk-admin-), not a project key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def buckets(session, params, max_pages=40):
    params = dict(params)
    for _ in range(max_pages):
        page = get(session, params)
        for bucket in page.get("data") or []:
            yield bucket
        if not page.get("has_more") or not page.get("next_page"):
            return
        params = dict(params)
        params["page"] = page["next_page"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days of daily cost buckets to read (default 30)")
    ap.add_argument("--threshold", type=float, default=0.50,
                    help="share above which one row is called dominant "
                         "(default 0.50)")
    ap.add_argument("--top", type=int, default=5,
                    help="rows to print per axis (default 5)")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_ADMIN_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key, read-only "
                  "scopes are enough)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})
    start = int(time.time()) - args.days * 86400

    found = 0
    for axis in AXES:
        rows = rank(list(buckets(session, {
            "start_time": start,
            "bucket_width": "1d",
            "limit": min(180, max(1, args.days)),
            "group_by": [axis],
        })), axis)
        state, detail = verdict(rows, args.threshold)
        line = "%-11s %-13s %s" % (axis, state, detail)

        if state in FINDINGS:
            found += 1
            log.warning(line)
        else:
            log.info(line)

        for row in rows[:args.top]:
            price = unit_price(row["amount"], row["quantity"], row["unit"])
            log.info("    %-38s $%10.2f  %5.1f%%  %s",
                     row["name"] if row["name"] is not None else "(no name)",
                     row["amount"], row["share"] * 100,
                     ("$%.2f per 1M tokens" % price) if price is not None
                     else "%s, not a token unit" % (row["unit"] or "no unit"))

        if state == "dominant" and axis == "line_item":
            log.warning("  repair: price the substitute for %r and run the "
                        "comparison before optimising anything else. Output "
                        "tokens are the expensive side on every current model, "
                        "and a smaller model at the same volume is usually a "
                        "multiple cheaper rather than a few percent.",
                        rows[0]["name"])
        elif state == "dominant" and axis == "project_id":
            log.warning("  repair: give project %r its own spend limit and its "
                        "own owner. A project this size behind the "
                        "organization's single ceiling means one loop in it can "
                        "stop everybody else's traffic.", rows[0]["name"])
        elif state == "unattributable":
            log.warning("  repair: this spend belongs to no %s. Move the traffic "
                        "onto named projects and keys before treating any "
                        "per-team number as real.", axis)

    log.info("2 axis/axes ranked, %d with a concentrated bill", found)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
