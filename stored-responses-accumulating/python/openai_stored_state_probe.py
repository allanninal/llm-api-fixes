"""Probe recorded response and conversation ids for retention and volume.

Read only. GET /v1/responses/{id}, GET /v1/conversations/{id} and
GET /v1/conversations/{id}/items. Nothing is created and nothing is deleted.

Neither /v1/responses nor /v1/conversations has a list endpoint, so there is no
way to enumerate what is stored. This probes the ids you recorded and prints a
coverage statement every run rather than implying it audited an account.

Two retention facts, both documented, both easy to quote the wrong way round.
Stored response data is kept for AT LEAST 30 days, which is a floor rather than
a deadline: an object you have not deleted is one you are still holding.
Conversations are retained UNTIL DELETED and their items are not deleted when
the conversation is.

The Response object does not echo `store` back, so a 200 is the only evidence
that a response was stored and a 404 has two causes, both of which are named.

This never follows previous_response_id. Walking a chain to see whether the
next turn resolves is a different question and a different script.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_stored_state_probe")

RESPONSES_URL = "https://api.openai.com/v1/responses"
CONVERSATIONS_URL = "https://api.openai.com/v1/conversations"

# Documented as "at least 30 days" for stored response data, and "until
# deleted" for conversations. Only the first is a number, and it is a floor.
RESPONSE_RETENTION_FLOOR_DAYS = 30
ITEM_PAGE = 100

FINDINGS = ("retained-past-policy", "items-outlive-response", "thread-unbounded",
            "thread-idle", "probe-unreadable")


def parse_records(text):
    """Route recorded ids by prefix. Pure. What cannot be routed is kept.

    Dropping an unroutable id would quietly shrink the denominator in a note
    whose whole subject is how little of the account it can see.
    """
    out = {"responses": [], "conversations": [], "unrecognised": []}
    seen = set()
    for line in str(text or "").splitlines():
        item = line.split("#", 1)[0].strip()
        if not item or item in seen:
            continue
        seen.add(item)
        if item.startswith("resp_"):
            out["responses"].append(item)
        elif item.startswith("conv_"):
            out["conversations"].append(item)
        else:
            out["unrecognised"].append(item)
    return out


def response_row(body):
    """One retrieved response, reduced. Pure. Five fields and no chain.

    There is deliberately no previous_response_id here. Walking upward from a
    response to its parent answers whether a thread still resolves, which is a
    different note; this one asks how old the object is and what it is attached
    to. There is no `store` field either, because the object does not carry one.
    """
    body = body if isinstance(body, dict) else {}
    conversation = body.get("conversation")
    if isinstance(conversation, dict):
        conversation = conversation.get("id")
    try:
        created = int(body.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0
    metadata = body.get("metadata")
    return {"id": str(body.get("id") or ""),
            "created_at": max(0, created),
            "status": str(body.get("status") or ""),
            "conversation": str(conversation or ""),
            "metadata_keys": len(metadata) if isinstance(metadata, dict) else 0}


def item_totals(items):
    """Count and the two timestamps that bound a thread. Pure."""
    stamps = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            at = int(item.get("created_at") or 0)
        except (TypeError, ValueError):
            at = 0
        if at > 0:
            stamps.append(at)
    return {"count": len(items or []),
            "oldest": min(stamps) if stamps else 0,
            "newest": max(stamps) if stamps else 0}


def age_days(when, now):
    """Age in days. Pure. The clock is an argument. None when undatable."""
    try:
        at, ref = int(when), int(now)
    except (TypeError, ValueError):
        return None
    return (ref - at) / 86400.0 if at > 0 else None


def grade_response(row, status, now, policy_days):
    """Grade one stored response against YOUR policy. Pure."""
    if status == 404:
        return ("not-retained",
                "nothing is stored under this id. It was created with store "
                "false, or it has already aged out")
    if status != 200:
        return ("probe-unreadable",
                "HTTP %s, so nothing about this id was established" % status)
    age = age_days((row or {}).get("created_at"), now)
    if age is None:
        return ("undatable",
                "stored, but it carried no usable created_at, so its age "
                "cannot be graded")
    conversation = str((row or {}).get("conversation") or "")
    if age > float(policy_days):
        tail = ("" if not conversation else
                ", and its items were added to conversation %s, which is "
                "retained until deleted" % conversation)
        return ("retained-past-policy",
                "still readable %.1f day(s) after creation, past your %d day "
                "policy. Retention is documented as at least %d days, so that "
                "is a floor and not a deadline%s"
                % (age, int(policy_days), RESPONSE_RETENTION_FLOOR_DAYS, tail))
    if conversation:
        return ("items-outlive-response",
                "%.1f day(s) old and inside your policy, but its items were "
                "added to conversation %s, which is retained until deleted"
                % (age, conversation))
    return ("within-policy",
            "stored, %.1f day(s) old, inside your %d day policy"
            % (age, int(policy_days)))


def grade_conversation(row, totals, status, now, policy_days, max_items):
    """Grade one conversation on volume first, then on idleness. Pure."""
    if status == 404:
        return ("not-retained",
                "no conversation under this id, so it has already been deleted")
    if status != 200:
        return ("probe-unreadable",
                "HTTP %s, so nothing about this id was established" % status)
    totals = totals or {"count": 0, "oldest": 0, "newest": 0}
    if int(totals.get("count") or 0) > int(max_items):
        return ("thread-unbounded",
                "%d item(s) and no TTL, so every turn on this thread carries "
                "them as input" % int(totals["count"]))
    idle = age_days(totals.get("newest"), now)
    if idle is not None and idle > float(policy_days):
        return ("thread-idle",
                "last item %.1f day(s) ago, past your %d day policy, and "
                "conversations are retained until deleted"
                % (idle, int(policy_days)))
    if idle is None:
        return ("thread-undatable",
                "%d item(s), none of which carried a usable created_at"
                % int(totals.get("count") or 0))
    return ("thread-within-policy",
            "%d item(s), last active %.1f day(s) ago"
            % (int(totals.get("count") or 0), idle))


def coverage_note(records):
    """The sentence that has to appear on every run. Pure."""
    records = records or {}
    return ("%d id(s) supplied: %d response(s), %d conversation(s), %d "
            "unroutable. Neither /v1/responses nor /v1/conversations has a "
            "list endpoint, so this is your records and not your account"
            % (sum(len(records.get(k) or []) for k in
                   ("responses", "conversations", "unrecognised")),
               len(records.get("responses") or []),
               len(records.get("conversations") or []),
               len(records.get("unrecognised") or [])))


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed."""
    items_first = ("delete the items first with DELETE /v1/conversations/"
                   "{conversation_id}/items/{item_id}, then the conversation. "
                   "Deleting the conversation does not delete its items.")
    if state == "retained-past-policy":
        return ["DELETE /v1/responses/{response_id} for what you no longer "
                "need, and pass store false on calls carrying regulated data.",
                "keep an id ledger with a created_at. It is the only inventory "
                "that can exist, because neither collection can be listed."]
    if state == "items-outlive-response":
        return ["deleting the response is not enough here. " + items_first]
    if state == "thread-unbounded":
        return ["start a fresh conversation seeded with a summary once a "
                "thread gets long, so input tokens stop compounding.",
                items_first]
    if state == "thread-idle":
        return [items_first]
    if state == "probe-unreadable":
        return ["the key could not read this id. Check that it belongs to the "
                "project that created the object before concluding anything "
                "about retention."]
    if state == "unrecognised-id":
        return ["route it by hand, or drop it. An id this script cannot "
                "classify is a hole in a coverage figure that is already "
                "bounded by your own records."]
    return []


def get_json(url, key, params=None, timeout=30):
    """One GET. Returns (status, body). A 404 is the answer, not an error."""
    try:
        r = requests.get(url, params=params or {},
                         headers={"Authorization": "Bearer " + key},
                         timeout=timeout)
    except requests.RequestException as exc:
        log.debug("GET %s failed: %s", url, exc)
        return (None, None)
    try:
        return (r.status_code, r.json())
    except ValueError:
        return (r.status_code, None)


def walk_items(conversation_id, key, max_pages):
    """Page a conversation's items on `after`. Returns (items, complete)."""
    url = "%s/%s/items" % (CONVERSATIONS_URL, conversation_id)
    items, cursor, pages = [], None, 0
    while pages < max_pages:
        params = {"limit": ITEM_PAGE, "order": "asc"}
        if cursor:
            params["after"] = cursor
        status, body = get_json(url, key, params)
        if status != 200 or not isinstance(body, dict):
            return (items, False)
        data = body.get("data") or []
        pages += 1
        items.extend(data)
        if not data or body.get("has_more") is False:
            return (items, True)
        cursor = data[-1].get("id")
        if not cursor:
            return (items, True)
    return (items, False)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--records", required=True,
                    help="file of recorded resp_ and conv_ ids, one per line")
    ap.add_argument("--policy-days", type=int, default=30,
                    help="your own retention rule, not the platform's")
    ap.add_argument("--max-items", type=int, default=500,
                    help="item count at which a thread stops being long")
    ap.add_argument("--max-item-pages", type=int, default=50,
                    help="page cap when counting one conversation's items")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key set to Read Only. Every "
                  "call is a GET of a response, a conversation or its items")
        return 2
    try:
        with open(args.records, "r", encoding="utf-8") as fh:
            records = parse_records(fh.read())
    except OSError as exc:
        log.error("could not read %s: %s", args.records, exc)
        return 2
    probed = len(records["responses"]) + len(records["conversations"])
    if not probed:
        log.error("no resp_ or conv_ ids in %s. Neither collection can be "
                  "listed, so the ids have to come from your own records",
                  args.records)
        return 2

    now = int(time.time())
    log.info("%s", coverage_note(records))
    findings = 0

    for rid in records["responses"]:
        status, body = get_json("%s/%s" % (RESPONSES_URL, rid), key)
        row = response_row(body)
        state, detail = grade_response(row, status, now, args.policy_days)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-22s %s: %s", state, rid, detail)
        for line in repair_lines(state):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    for cid in records["conversations"]:
        status, body = get_json("%s/%s" % (CONVERSATIONS_URL, cid), key)
        totals = None
        if status == 200:
            items, complete = walk_items(cid, key, args.max_item_pages)
            totals = item_totals(items)
            if not complete:
                log.warning("%-22s %s: the item listing stopped early, so %d "
                            "is a floor", "items-incomplete", cid,
                            totals["count"])
        state, detail = grade_conversation(body, totals, status, now,
                                           args.policy_days, args.max_items)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-22s %s: %s", state, cid, detail)
        for line in repair_lines(state):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    for other in records["unrecognised"]:
        log.info("%-22s %s: neither a resp_ nor a conv_ id, so it was not "
                 "probed", "unrecognised-id", other)

    log.info("%d supplied, %d probed, %d finding(s)",
             probed + len(records["unrecognised"]), probed, findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
