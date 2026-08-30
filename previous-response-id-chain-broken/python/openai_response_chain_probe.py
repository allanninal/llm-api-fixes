"""Walk recorded previous_response_id chains and find the links already gone.

Read only. One GET of /v1/responses/{response_id} per link, and nothing else.
No completion is created, nothing is stored and nothing is deleted.

Response objects are saved for 30 days by default, so a chain is exactly as
durable as its oldest surviving link. This walks upward from the newest id you
recorded to a root or to a gap, and reports the runway from the oldest link
rather than from the newest.

The one documented exception is also the repair: a response attached to a
conversation has its items persisted with no 30 day TTL, so a conversation
backed chain keeps its history whatever happens to the response objects.

/v1/responses has no list endpoint, so the ids come from your own records and
every verdict is bounded by the chains you supply.
"""
import argparse
import logging
import os
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_response_chain_probe")

BASE_URL = "https://api.openai.com/v1/responses"

# The documented default: response objects are saved for 30 days.
RETENTION_DAYS = 30

FINDINGS = ("chain-broken", "chain-expiring", "chain-unreadable")


def parse_ids(text):
    """Response ids from a file. Pure. Blanks, comments and repeats dropped."""
    seen = []
    for line in str(text or "").splitlines():
        item = line.split("#", 1)[0].strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def link_row(body):
    """One retrieved response, reduced. Pure. Four fields and no invention.

    There is deliberately no "stored" field here. The Response object does not
    carry a store flag, so the only honest evidence that a response was stored
    is that retrieving it worked, and that is recorded as the status code.
    """
    body = body if isinstance(body, dict) else {}
    conversation = body.get("conversation")
    if isinstance(conversation, dict):
        conversation = conversation.get("id")
    try:
        created = int(body.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0
    return {"id": str(body.get("id") or ""),
            "created_at": created,
            "previous_response_id": str(body.get("previous_response_id") or ""),
            "conversation": str(conversation or ""),
            "status": str(body.get("status") or "")}


def age_days(created_at, now):
    """Age of one link in days. Pure. The clock is an argument."""
    try:
        created = int(created_at)
        now = int(now)
    except (TypeError, ValueError):
        return None
    if created <= 0:
        return None
    return (now - created) / 86400.0


def oldest_link(chain):
    """The link that decides the chain. Pure. None for an empty chain."""
    usable = [row for row in chain or [] if int(row.get("created_at") or 0) > 0]
    if not usable:
        return None
    return min(usable, key=lambda row: int(row["created_at"]))


def runway_days(chain, now, retention=RETENTION_DAYS):
    """Days left on the oldest link. Pure. None when nothing is datable."""
    row = oldest_link(chain)
    if row is None:
        return None
    age = age_days(row["created_at"], now)
    if age is None:
        return None
    return retention - age


def classify_chain(head, chain, gap, unreadable, truncated, now, warn_days):
    """Grade one walked chain. Pure. Returns (state, detail)."""
    if unreadable:
        return ("chain-unreadable",
                "%s: %s, so nothing about this chain was established"
                % (head, unreadable))
    if gap:
        others = len(chain)
        if others:
            return ("chain-broken",
                    "%s: the parent %s no longer resolves, so the next turn on "
                    "this thread will 404" % (head, gap))
        return ("chain-broken",
                "%s: this id itself does not resolve. It has either aged out of "
                "the 30 day retention or was never stored" % head)
    if not chain:
        return ("nothing-walked", "%s: no links were read" % head)

    conversations = {row.get("conversation") for row in chain}
    if conversations and "" not in conversations:
        return ("conversation-backed",
                "%s: items attached to a conversation are persisted with no 30 "
                "day TTL" % head)

    left = runway_days(chain, now)
    if left is None:
        return ("undatable",
                "%s: no link carried a usable created_at, so the runway cannot "
                "be computed" % head)
    if left <= 0:
        return ("chain-broken",
                "%s: the oldest link is past the documented %d day retention "
                "and is only resolving on borrowed time"
                % (head, RETENTION_DAYS))
    if left <= warn_days:
        row = oldest_link(chain)
        return ("chain-expiring",
                "%s: the oldest link is %.1f days old, so this chain has about "
                "%.1f days of the documented %d day retention left"
                % (head, age_days(row["created_at"], now), left, RETENTION_DAYS))
    if truncated:
        return ("chain-unfinished",
                "%s: stopped at the hop limit before reaching a root, so the "
                "oldest link was never seen" % head)
    return ("chain-intact",
            "%s: walked to a root with %.1f days left on the oldest link"
            % (head, left))


def repair_lines(state):
    """The repair for one verdict. Pure. Printed, never performed."""
    move = ("move this thread onto a conversation object, whose items are "
            "persisted with no 30 day TTL, or keep the full message history in "
            "your own store and replay it.")
    if state == "chain-broken":
        return ["fall back to replaying local history for this thread, and stop "
                "chaining from an id you did not verify.", move]
    if state == "chain-expiring":
        return [move,
                "until then, verify the parent resolves before continuing an "
                "old thread rather than discovering it inside a user request."]
    if state == "chain-unreadable":
        return ["the key could not read this response. Check that it belongs to "
                "the project that created it before concluding anything about "
                "retention."]
    if state == "chain-unfinished":
        return ["raise --max-hops for this thread. A chain graded without "
                "reaching its oldest link has not been graded."]
    if state == "undatable":
        return ["the links resolved but carried no created_at, which is odd "
                "enough to read one of them by hand before trusting the rest."]
    return []


def retrieve(response_id, key, timeout=30):
    """One GET. Returns (status, body). A 404 is the answer, not an error."""
    try:
        r = requests.get("%s/%s" % (BASE_URL, response_id),
                         headers={"Authorization": "Bearer " + key},
                         timeout=timeout)
    except requests.RequestException as exc:
        log.debug("retrieve of %s failed: %s", response_id, exc)
        return (None, None)
    try:
        return (r.status_code, r.json())
    except ValueError:
        return (r.status_code, None)


def walk(head, key, max_hops):
    """Follow one chain upward. Returns (chain, gap, unreadable, truncated)."""
    chain = []
    current = head
    for _ in range(max_hops):
        status, body = retrieve(current, key)
        if status == 404:
            return (chain, current, "", False)
        if status in (401, 403):
            return (chain, "", "HTTP %d reading %s" % (status, current), False)
        if status != 200:
            return (chain, "", "HTTP %s reading %s" % (status, current), False)
        row = link_row(body)
        chain.append(row)
        if not row["previous_response_id"]:
            return (chain, "", "", False)
        current = row["previous_response_id"]
    return (chain, "", "", True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", required=True,
                    help="file of response ids, newest per thread, one per line")
    ap.add_argument("--max-hops", type=int, default=20,
                    help="how far up one chain to walk")
    ap.add_argument("--warn-days", type=float, default=5.0,
                    help="days of remaining retention that count as a finding")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log.error("set OPENAI_API_KEY to a project key set to Read Only. Every "
                  "call is a GET of /v1/responses/{response_id}")
        return 2
    try:
        with open(args.ids, "r", encoding="utf-8") as fh:
            heads = parse_ids(fh.read())
    except OSError as exc:
        log.error("could not read %s: %s", args.ids, exc)
        return 2
    if not heads:
        log.error("no response ids in %s. /v1/responses cannot be listed, so "
                  "the chains have to start from ids you recorded", args.ids)
        return 2

    now = int(time.time())
    findings = 0
    for head in heads:
        chain, gap, unreadable, truncated = walk(head, key, args.max_hops)
        row = oldest_link(chain)
        if row:
            age = age_days(row["created_at"], now)
            log.info("%-10s chain of %d, oldest %s, %.1f days old",
                     head, len(chain), row["id"], age or 0.0)
        else:
            log.info("%-10s chain of %d", head, len(chain))

        state, detail = classify_chain(head, chain, gap, unreadable, truncated,
                                       now, args.warn_days)
        emit = log.warning if state in FINDINGS else log.info
        emit("%-20s %s", state, detail)
        for line in repair_lines(state):
            emit("  repair: %s", line)
        if state in FINDINGS:
            findings += 1

    log.info("%d chain(s) walked, %d finding(s)", len(heads), findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
