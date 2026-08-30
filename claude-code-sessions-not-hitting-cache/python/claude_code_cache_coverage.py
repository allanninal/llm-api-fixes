"""Find Claude Code actors whose sessions never read a cached prefix.

Read only. One paged GET per UTC day against the Claude Code usage report with
an Admin API key. No message is sent and nothing is written.

This is a different report from the messages usage report. Its unit is an actor
and a day, it carries session counts and tool actions that exist nowhere else,
and it cannot be joined to the other report by any field. Two token names are
the same; nothing else is.

The report covers Claude Code on the Claude API only. Usage through Bedrock,
Google Cloud, Foundry or Claude Platform on AWS is not reported here, so a
finding of "no evidence" is not a finding of "no problem" for those paths.

No savings figure is printed. Cache reads bill at 0.1x, but the report does not
say how much of tokens.input was reusable prefix and how much was genuinely
new, and that ratio is the entire calculation.
"""
import argparse
import datetime as dt
import decimal
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude_code_cache_coverage")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

FINDINGS = ("no-cache-at-all", "writes-never-read", "thin-cache")


def actor_name(record):
    """Who the record belongs to. Pure. Both actor shapes, plus neither.

    A user actor carries an email address; an api actor carries a key name.
    A reader that knows only the first drops every service account silently,
    which is the half of the roster nobody is watching.
    """
    actor = (record or {}).get("actor")
    if not isinstance(actor, dict):
        return "unattributed"
    for field in ("email_address", "api_key_name"):
        value = str(actor.get(field) or "").strip()
        if value:
            return value
    return "unattributed"


def mask(name):
    """Hide the local part of an email address. Pure. Non-emails pass through.

    The actor on this report is usually a person, and a per-developer cost
    table is personal data. Masking by default costs nothing and makes the
    output safe to paste into a channel.
    """
    text = str(name or "").strip()
    if "@" not in text:
        return text or "unattributed"
    local, _, domain = text.partition("@")
    if not local:
        return text
    return local[0] + "***@" + domain


def tokens_of(entry):
    """The four token counts off one model_breakdown entry. Pure."""
    tokens = (entry or {}).get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    out = {}
    for field in ("input", "output", "cache_read", "cache_creation"):
        try:
            out[field] = max(0, int(tokens.get(field) or 0))
        except (TypeError, ValueError):
            out[field] = 0
    return out


def cost_cents(entry):
    """estimated_cost.amount as a Decimal of cents. Pure. 0 when unreadable.

    A decimal string, parsed as a decimal. Money through a float is how a
    per-developer table ends up disagreeing with itself in the third place.
    """
    cost = (entry or {}).get("estimated_cost")
    cost = cost if isinstance(cost, dict) else {}
    try:
        return decimal.Decimal(str(cost.get("amount") or "0"))
    except (decimal.InvalidOperation, ValueError):
        return decimal.Decimal("0")


def fold(pages):
    """Fold every record into one row per actor. Pure.

    Sums across model_breakdown[] rather than reading the first entry, and
    sums sessions across days. An actor who used two models in one day has two
    entries, and taking the first understates them by whatever the second held.
    """
    rows = {}
    for page in pages or []:
        for record in (page or {}).get("data") or []:
            if not isinstance(record, dict):
                continue
            who = actor_name(record)
            row = rows.setdefault(who, {
                "sessions": 0, "days": 0, "input": 0, "output": 0,
                "cache_read": 0, "cache_creation": 0,
                "cents": decimal.Decimal("0"), "models": set()})
            row["days"] += 1
            core = record.get("core_metrics")
            core = core if isinstance(core, dict) else {}
            try:
                row["sessions"] += max(0, int(core.get("num_sessions") or 0))
            except (TypeError, ValueError):
                pass
            for entry in record.get("model_breakdown") or []:
                counts = tokens_of(entry)
                for field, value in counts.items():
                    row[field] += value
                row["cents"] += cost_cents(entry)
                model = str((entry or {}).get("model") or "").strip()
                if model:
                    row["models"].add(model)
    return rows


def read_share(row):
    """Share of an actor's input that was read back from cache. Pure.

    Reads over reads plus uncached input. Cache writes are deliberately not in
    the denominator: they are a cost, not a hit, and putting them there makes a
    prefix that is written and never matched look partly cached.
    """
    data = row or {}
    reads = max(0, int(data.get("cache_read") or 0))
    fresh = max(0, int(data.get("input") or 0))
    total = reads + fresh
    if total <= 0:
        return 0.0
    return reads / float(total)


def cost_per_session(row):
    """Cents per session for one actor. Pure. None when there are no sessions."""
    data = row or {}
    sessions = max(0, int(data.get("sessions") or 0))
    if sessions <= 0:
        return None
    return decimal.Decimal(data.get("cents") or 0) / decimal.Decimal(sessions)


def verdict(row, min_sessions=2, min_input=100_000, floor=0.10):
    """Classify one actor's cache behaviour. Pure. Returns (state, detail)."""
    data = row or {}
    sessions = max(0, int(data.get("sessions") or 0))
    reads = max(0, int(data.get("cache_read") or 0))
    writes = max(0, int(data.get("cache_creation") or 0))
    fresh = max(0, int(data.get("input") or 0))

    if sessions < min_sessions:
        return ("too-few-sessions",
                "%d session(s) in the window: there was no earlier turn for a "
                "prefix to be read back from, so a zero here is arithmetic "
                "rather than a finding" % sessions)
    if reads + fresh < min_input:
        return ("low-volume",
                "%d session(s) and %d input token(s), too few to conclude "
                "anything" % (sessions, reads + fresh))

    share = read_share(data)
    if reads == 0 and writes == 0:
        return ("no-cache-at-all",
                "%d session(s), 0%% of input read from cache, and no cache "
                "writes either: the prefix is never being cached at all"
                % sessions)
    if reads == 0:
        return ("writes-never-read",
                "%d session(s), 0%% read with %.1fM token(s) written: entries "
                "are being created at a premium and never matched"
                % (sessions, writes / 1e6))
    if share < floor:
        return ("thin-cache",
                "%d session(s), %.0f%% of input read from cache, under the "
                "floor of %.0f%%" % (sessions, share * 100, floor * 100))
    return ("cached",
            "%d session(s), %.0f%% of input read from cache"
            % (sessions, share * 100))


def repair_lines(state):
    """The repair for one classified actor. Pure. Printed, never performed."""
    if state == "no-cache-at-all":
        return [
            "check whether these sessions are one prompt each. A prefix is "
            "only reusable across turns of the same session, so a fresh "
            "session per question pays full rate for the project context, the "
            "tool definitions and every file already read.",
            "continuing a session rather than starting one is the whole fix, "
            "and it is a habit rather than a setting.",
        ]
    if state == "writes-never-read":
        return [
            "entries are being written and never matched, so something ahead "
            "of the stable block is changing between turns.",
            "this is the more expensive of the two zeros: cache writes cost "
            "more than plain input, so the current state is worse than not "
            "caching at all.",
        ]
    if state == "thin-cache":
        return [
            "some turns are matching and most are not. Look for a mix of long "
            "sessions and one-shot invocations under the same actor before "
            "concluding the prefix is unstable.",
        ]
    return []


def get(session, path, params):
    r = session.get(API + path, params=params, timeout=60)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: /v1/organizations/* needs an Admin "
                         "API key (sk-ant-admin...), not a workspace key"
                         % r.status_code)
    r.raise_for_status()
    return r.json()


def pages(session, path, params):
    """Walk one day of the paginated Claude Code usage report."""
    params = dict(params)
    while True:
        page = get(session, path, params)
        yield page
        if not page.get("has_more") or not page.get("next_page"):
            return
        params["page"] = page["next_page"]


def day_strings(days, today=None):
    """The UTC dates to request, newest first. Pure. Today is excluded.

    Only records older than an hour are returned, so today is always a partial
    day, and a partial day reads as a quiet one.
    """
    end = today or dt.datetime.now(dt.timezone.utc).date()
    return [(end - dt.timedelta(days=n)).isoformat()
            for n in range(1, max(1, int(days)) + 1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7,
                    help="UTC days to read, ending yesterday (default 7)")
    ap.add_argument("--min-sessions", type=int, default=2,
                    help="sessions below which no claim is made (default 2)")
    ap.add_argument("--floor", type=float, default=0.10,
                    help="cache read share below which coverage is thin")
    ap.add_argument("--show-actors", action="store_true",
                    help="print email addresses in full instead of masked")
    ap.add_argument("--show-all", action="store_true",
                    help="also print actors whose caching is healthy")
    args = ap.parse_args()

    admin = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not admin:
        log.error("set ANTHROPIC_ADMIN_KEY to an Admin API key (sk-ant-admin...); "
                  "a workspace key cannot read /v1/organizations/*")
        return 2

    s = requests.Session()
    s.headers.update({"x-api-key": admin, "anthropic-version": VERSION})

    collected = []
    days = day_strings(args.days)
    for day in days:
        collected.extend(pages(s, "/organizations/usage_report/claude_code",
                               {"starting_at": day, "limit": 1000}))

    rows = fold(collected)
    if not rows:
        log.info("no Claude Code records over %d day(s). This report covers "
                 "Claude Code on the Claude API only: Bedrock, Google Cloud, "
                 "Foundry and Claude Platform on AWS usage is not here.",
                 len(days))
        return 0

    bad = 0
    for who in sorted(rows, key=lambda a: -rows[a]["cents"]):
        row = rows[who]
        state, detail = verdict(row, args.min_sessions, floor=args.floor)
        label = who if args.show_actors else mask(who)
        line = "%-20s %-22s %s, $%.2f" % (state, label, detail,
                                          float(row["cents"]) / 100.0)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
            for repair in repair_lines(state):
                log.warning("  repair: %s", repair)
        elif args.show_all or state != "cached":
            log.info(line)

    log.info("%d actor(s) over %d day(s), %d finding(s)",
             len(rows), len(days), bad)
    log.info("no savings figure: the report does not say how much of "
             "tokens.input was reusable prefix, and that ratio is the whole "
             "calculation")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
