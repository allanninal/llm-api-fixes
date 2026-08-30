"""Read the key and member lifecycle events nobody has ever read.

Read only. GET requests only, against the OpenAI Audit Logs API and the
Anthropic Compliance activity feed. Nothing is created, changed or removed.

Both feeds are pull-only: there is no webhook, no email and no default alert on
either provider, which is why the control exists everywhere and has fired
nowhere. The finding is not any single event; it is that nobody is reading. So
the last thing this prints is a watermark to store for the next run.

Two honest limits are enforced in the code rather than mentioned in a comment.
An empty feed is reported as unavailable and never as clean, because audit
logging is gated to organizations that have it enabled. And the geography rule
runs on OpenAI session actors only: the Anthropic activity record carries an
email, a user id, an IP and a user agent, and no country breakdown to test.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("llm_key_lifecycle_review")

OPENAI = "https://api.openai.com/v1"
ANTHROPIC = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

OPENAI_EVENTS = ("api_key.created", "api_key.updated", "api_key.deleted",
                 "service_account.created", "service_account.deleted",
                 "login.failed")

OFF_ROSTER = "off-roster-actor"
UNATTRIBUTABLE = "unattributable"
UNEXPECTED_COUNTRY = "unexpected-country"
OUT_OF_HOURS = "out-of-hours"
REVIEWED = "reviewed"
FINDINGS = (OFF_ROSTER, UNATTRIBUTABLE, UNEXPECTED_COUNTRY, OUT_OF_HOURS)

FEED_OK = "feed-readable"
FEED_UNAVAILABLE = "feed-unavailable"

# Highest first. An event can trip several rules at once and every reason is
# printed; the state is the one that decides how loudly it is printed.
SEVERITY = (OFF_ROSTER, UNEXPECTED_COUNTRY, UNATTRIBUTABLE, OUT_OF_HOURS)


def parse_when(value):
    """Epoch seconds from a unix integer or an RFC 3339 string. Pure.

    OpenAI dates effective_at in unix seconds; the Anthropic activity record
    uses an RFC 3339 string. Normalising here is what lets one grader read both
    feeds without either one being special-cased downstream.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return int(when.timestamp())


def iso(epoch):
    """A readable UTC timestamp. Pure."""
    if epoch is None:
        return "(no timestamp)"
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc) \
             .strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_openai(entry):
    """One audit-log entry in the common shape. Pure.

    The actor arrives in two shapes and they keep the email in different
    places. A session actor is the forensically rich one and carries the
    address, the IP and ip_address_details. An api_key actor carries a tracking
    id and either a user email or a service account id, and frequently no email
    at all, which is a distinct outcome rather than a missing value.
    """
    row = entry or {}
    actor = row.get("actor") if isinstance(row.get("actor"), dict) else {}
    kind = str(actor.get("type") or "").strip().lower()
    email, ip, country = None, None, None
    if kind == "session":
        session = actor.get("session") if isinstance(actor.get("session"), dict) else {}
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        email = user.get("email")
        ip = session.get("ip_address")
        details = (session.get("ip_address_details")
                   if isinstance(session.get("ip_address_details"), dict) else {})
        country = details.get("country")
    elif kind == "api_key":
        api_key = actor.get("api_key") if isinstance(actor.get("api_key"), dict) else {}
        user = api_key.get("user") if isinstance(api_key.get("user"), dict) else {}
        email = user.get("email")
    project = row.get("project") if isinstance(row.get("project"), dict) else {}
    return {"source": "openai", "type": str(row.get("type") or "(untyped)"),
            "when": parse_when(row.get("effective_at")),
            "actor_kind": kind or "unknown",
            "actor_email": str(email).strip().lower() if email else None,
            "actor_ip": ip, "country": country,
            "container": project.get("name") or project.get("id")}


def normalise_anthropic(activity):
    """One compliance activity in the common shape. Pure.

    country stays None on purpose. The activity record carries email_address,
    user_id, ip_address and user_agent and no geography breakdown, and leaving
    the field absent is how the grader knows to skip the country rule here
    rather than silently passing every Anthropic event.
    """
    row = activity or {}
    actor = row.get("actor") if isinstance(row.get("actor"), dict) else {}
    email = actor.get("email_address")
    return {"source": "anthropic", "type": str(row.get("type") or "(untyped)"),
            "when": parse_when(row.get("created_at")),
            "actor_kind": "user" if email else "unknown",
            "actor_email": str(email).strip().lower() if email else None,
            "actor_ip": actor.get("ip_address"), "country": None,
            "container": row.get("organization_id")}


def resolve_actor(event, roster):
    """on-roster, off-roster or unattributable. Pure.

    The join that turns a feed into a finding. An email checked against the
    current roster is the difference between an event a reviewer can close and
    an action taken by somebody whose access has since ended.
    """
    email = (event or {}).get("actor_email")
    if not email:
        return "unattributable"
    return "on-roster" if str(email).strip().lower() in (roster or set()) \
        else "off-roster"


def hour_of(event):
    """The UTC hour of an event, or None. Pure."""
    when = (event or {}).get("when")
    if when is None:
        return None
    return dt.datetime.fromtimestamp(int(when), dt.timezone.utc).hour


def grade(event, roster, business_hours=(7, 19), operating_countries=None):
    """Classify one normalised event. Pure. Returns (state, reasons).

    Every rule that fires contributes a reason, because an event can be
    off-roster and out of hours and from an unexpected country at once and a
    reviewer wants all three. The state is the most severe reason present and
    decides how loudly the row is printed.
    """
    reasons = []
    resolution = resolve_actor(event, roster)
    if resolution == "off-roster":
        reasons.append((OFF_ROSTER, "the actor is not on the current roster"))
    elif resolution == "unattributable":
        reasons.append((UNATTRIBUTABLE,
                        "an %s actor carries no user email, so no person can "
                        "be attributed" % ((event or {}).get("actor_kind")
                                           or "unknown")))

    country = (event or {}).get("country")
    if operating_countries and country:
        if str(country).strip().upper() not in {c.upper() for c in operating_countries}:
            reasons.append((UNEXPECTED_COUNTRY,
                            "ip_address_details.country %s is outside the "
                            "operating geographies" % country))

    hour = hour_of(event)
    start, end = business_hours
    creation = str((event or {}).get("type") or "").endswith((".created", ".deleted"))
    if creation and hour is not None and not (start <= hour < end):
        reasons.append((OUT_OF_HOURS,
                        "created outside business hours (%02d:00 UTC)" % hour))

    if not reasons:
        return (REVIEWED, [])
    present = {state for state, _ in reasons}
    state = next(s for s in SEVERITY if s in present)
    return (state, [text for _, text in reasons])


def feed_state(events, reachable):
    """Whether the feed said anything at all. Pure. (state, detail).

    An empty feed is the most misreadable result on this surface. Audit logging
    is gated to organizations that have it enabled, so treating silence as "no
    findings" turns a missing control into a passing check.
    """
    if not reachable:
        return (FEED_UNAVAILABLE,
                "the feed could not be read, so nothing below is a review of "
                "anything")
    if not (events or []):
        return (FEED_UNAVAILABLE,
                "the feed returned no events at all. Audit logging is gated to "
                "organizations that have it enabled, so this is not a clean "
                "result: it is an unknown one.")
    return (FEED_OK, "%d event(s) read" % len(events))


def failed_login_bursts(events, window_seconds=600, threshold=5):
    """Clusters of login.failed inside one window. Pure.

    A single failed login is a typo. Five in ten minutes is the only pattern in
    this feed that is worth an alert on its own rather than a weekly read.
    """
    rows = sorted([e for e in (events or [])
                   if str((e or {}).get("type") or "") == "login.failed"
                   and (e or {}).get("when") is not None],
                  key=lambda e: e["when"])
    bursts = []
    for i, first in enumerate(rows):
        window = [e for e in rows[i:]
                  if e["when"] - first["when"] <= window_seconds]
        if len(window) >= threshold:
            bursts.append((first["when"], len(window),
                           first.get("actor_email") or "(no email)"))
            break
    return bursts


def watermark(events):
    """The newest timestamp seen, for the next run's cursor. Pure."""
    stamps = [e["when"] for e in (events or []) if (e or {}).get("when") is not None]
    return max(stamps) if stamps else None


def project_caveat(event):
    """Whether this entry's project field means anything. Pure.

    Admin actions taken with an Admin API key are attributed to the default
    project, so the project on those entries says nothing about where the
    action landed.
    """
    if (event or {}).get("source") != "openai":
        return None
    if (event or {}).get("actor_kind") == "api_key":
        return ("project is not meaningful here: admin actions taken with an "
                "Admin API key are attributed to the default project")
    return None


def get(session, url, params, who):
    r = session.get(url, params=params, timeout=60)
    if r.status_code == 429:
        raise SystemExit("429 from %s: this feed declares its own rate limit "
                         "with Retry-After. Back off and resume from the "
                         "stored watermark." % who)
    if r.status_code in (401, 403):
        raise SystemExit("%d from %s: the feed needs an administration "
                         "credential, and on Anthropic the "
                         "read:compliance_activities scope" % (r.status_code, who))
    r.raise_for_status()
    return r.json()


def collect(session, url, params, who, cursor="after", limit_pages=20):
    rows = []
    params = dict(params)
    for _ in range(limit_pages):
        page = get(session, url, params, who)
        rows.extend(page.get("data") or [])
        if not page.get("has_more") or not page.get("last_id"):
            return rows
        params[cursor] = page["last_id"]
    return rows


def report(name, events, roster, args, geography):
    state, detail = feed_state(events, True)
    log.info("%s: %s (%s), roster of %d member(s); %s", name, state, detail,
             len(roster), "country and session rules available" if geography
             else "no geography on this feed")
    if state == FEED_UNAVAILABLE:
        return 0

    countries = [c.strip() for c in args.countries.split(",") if c.strip()]
    findings = 0
    for event in sorted(events, key=lambda e: e.get("when") or 0):
        verdict, reasons = grade(event, roster,
                                 (args.hours_from, args.hours_to),
                                 countries if geography else None)
        if verdict == REVIEWED:
            continue
        findings += 1
        log.warning("%-19s %-22s %s  %-18s %-15s %s", verdict, event["type"],
                    iso(event.get("when")),
                    event.get("actor_email") or "(%s actor)" % event.get("actor_kind"),
                    event.get("actor_ip") or "-", event.get("country") or "")
        for reason in reasons:
            log.warning("  reason: %s", reason)
        caveat = project_caveat(event)
        if caveat:
            log.info("  note: %s", caveat)

    for when, count, who in failed_login_bursts(events):
        findings += 1
        log.warning("login-failed-burst   %d failure(s) within 10 minutes from "
                    "%s, starting %s", count, who, iso(when))

    mark = watermark(events)
    if mark is not None:
        log.info("watermark: store the cursor %d (%s) for the next %s run",
                 mark, iso(mark), name)
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7,
                    help="days of lifecycle events to read (default 7)")
    ap.add_argument("--hours-from", type=int, default=7,
                    help="first business hour, UTC")
    ap.add_argument("--hours-to", type=int, default=19,
                    help="first non-business hour, UTC")
    ap.add_argument("--countries", default="US,GB,DE,IE",
                    help="comma separated operating geographies for the "
                         "country rule, which runs on OpenAI events only")
    args = ap.parse_args()

    openai_key = os.environ.get("OPENAI_ADMIN_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if not openai_key and not anthropic_key:
        log.error("set OPENAI_ADMIN_KEY or ANTHROPIC_ADMIN_KEY, or both; the "
                  "Anthropic credential also needs the "
                  "read:compliance_activities scope")
        return 2

    since = int((dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=args.days)).timestamp())
    findings = 0

    if openai_key:
        s = requests.Session()
        s.headers.update({"Authorization": "Bearer " + openai_key})
        roster = {str(u.get("email") or "").strip().lower()
                  for u in collect(s, OPENAI + "/organization/users",
                                   {"limit": 100}, "OpenAI")
                  if u.get("email")}
        raw = collect(s, OPENAI + "/organization/audit_logs",
                      {"limit": 100, "effective_at[gte]": since,
                       "event_types[]": list(OPENAI_EVENTS)}, "OpenAI")
        findings += report("openai", [normalise_openai(e) for e in raw],
                           roster, args, geography=True)

    if anthropic_key:
        s = requests.Session()
        s.headers.update({"x-api-key": anthropic_key,
                          "anthropic-version": ANTHROPIC_VERSION})
        roster = {str(u.get("email") or "").strip().lower()
                  for u in collect(s, ANTHROPIC + "/organizations/users",
                                   {"limit": 1000}, "Anthropic",
                                   cursor="after_id")
                  if u.get("email")}
        raw = collect(s, ANTHROPIC + "/compliance/activities", {"limit": 100},
                      "Anthropic")
        findings += report("anthropic",
                           [normalise_anthropic(a) for a in raw
                            if (parse_when((a or {}).get("created_at")) or 0) >= since],
                           roster, args, geography=False)

    log.info("%d finding(s)", findings)
    log.info("the repair is a schedule, not a run: poll from the stored "
             "watermark and route these events to somewhere a person looks")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
