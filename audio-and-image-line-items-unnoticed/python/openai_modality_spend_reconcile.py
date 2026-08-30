"""Reconcile an OpenAI token dashboard against the whole bill.

Read only. GET requests and nothing else: OPENAI_ADMIN_KEY must be an
organization admin key (sk-admin-...) with read scopes.

Costs is the only endpoint denominated in money. The per-modality usage
endpoints are denominated in characters, seconds, images, sessions and calls,
and a dashboard built on completions can see none of them. This script prints
the difference and stops.
"""
import argparse
import datetime as dt
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openai_modality_spend_reconcile")

API = "https://api.openai.com/v1"

# Every usage surface, with the field it is denominated in. Five different units
# across eight endpoints is the reason a token dashboard cannot be made complete
# by adding one more query to it.
SURFACES = (
    ("completions", "/organization/usage/completions", "num_model_requests", "requests"),
    ("embeddings", "/organization/usage/embeddings", "input_tokens", "tokens"),
    ("moderations", "/organization/usage/moderations", "input_tokens", "tokens"),
    ("audio_speeches", "/organization/usage/audio_speeches", "characters", "characters"),
    ("audio_transcriptions", "/organization/usage/audio_transcriptions", "seconds", "seconds"),
    ("images", "/organization/usage/images", "images", "images"),
    ("code_interpreter_sessions", "/organization/usage/code_interpreter_sessions",
     "num_sessions", "sessions"),
    ("file_search_calls", "/organization/usage/file_search_calls", "num_requests", "calls"),
    ("web_search_calls", "/organization/usage/web_search_calls", "num_requests", "calls"),
)

# Matched in order against a lowercased line_item. Audio, image and tool come
# before text because "gpt-image-1" and "gpt-4o-audio-preview" both contain a
# text-model substring and neither is billed in text tokens.
FAMILIES = (
    ("audio", ("audio", "speech", "transcription", "whisper", "tts", "realtime")),
    ("image", ("image", "dall-e")),
    ("tool", ("web search", "web_search", "file search", "file_search",
              "code interpreter", "code_interpreter", "container")),
    ("embedding", ("embedding",)),
    ("moderation", ("moderation",)),
    ("text", ("input tokens", "output tokens", "cached input", "cached_input",
              "gpt-", "o1-", "o3", "o4-", "chat")),
)

# The token types that hide inside a completions result. Adding input_tokens and
# output_tokens whole treats every one of these as the same money.
MIXED_TOKEN_FIELDS = ("input_audio_tokens", "output_audio_tokens",
                      "input_image_tokens", "output_image_tokens")

FINDINGS = ("gap", "unclassified-line-items")


def family(line_item):
    """Map a cost report line_item onto a modality family. Pure.

    Returns "other" for anything unrecognised, and "other" is deliberately loud
    rather than a quiet bucket: the platform ships new billable surfaces, and a
    reconciliation that silently absorbs the next one is worse than none.
    """
    name = str(line_item or "").strip().lower()
    if not name:
        return "other"
    for label, markers in FAMILIES:
        if any(marker in name for marker in markers):
            return label
    return "other"


def reconcile(items, covers):
    """Split spend into what the dashboard covers and what it does not. Pure.

    items is [(line_item, amount, quantity, quantity_unit), ...] as read off
    GET /v1/organization/costs grouped by line_item. covers is the set of family
    names your dashboard actually renders. Amounts that will not parse are
    counted as unreadable rather than as zero, because zero would shrink the gap.
    """
    out = {"total": 0.0, "covered": 0.0, "uncovered": 0.0, "unreadable": 0,
           "by_family": {}, "rows": []}
    wanted = {str(c).strip().lower() for c in covers}
    for line_item, amount, quantity, unit in items:
        try:
            value = float(amount)
        except (TypeError, ValueError):
            out["unreadable"] += 1
            continue
        label = family(line_item)
        out["total"] += value
        out["by_family"][label] = out["by_family"].get(label, 0.0) + value
        if label in wanted:
            out["covered"] += value
        else:
            out["uncovered"] += value
            out["rows"].append((label, str(line_item), value, quantity, unit))
    out["rows"].sort(key=lambda r: -r[2])
    return out


def verdict(recon, tolerance=0.02):
    """Is the remainder rounding or a hole? Pure. Returns (state, detail).

    tolerance is a fraction of total spend, defaulting to 2%, which is about
    where a gap stops being explicable as timing and lag. A gap made mostly of
    line items the script could not classify gets its own state, because the
    repair is to go and read the strings rather than to add a known endpoint.
    """
    total = recon.get("total") or 0.0
    uncovered = recon.get("uncovered") or 0.0
    if total <= 0:
        return ("no-spend",
                "no spend in the window, so there is nothing to reconcile")

    share = uncovered / total
    money = ("$%.2f total, $%.2f (%.1f%%) outside what the dashboard covers"
             % (total, uncovered, share * 100))

    if share < tolerance:
        return ("reconciled",
                "%s, inside the %.1f%% tolerance" % (money, tolerance * 100))

    # Derived from the uncovered rows rather than from by_family, because
    # by_family counts both sides and the question here is only about the half
    # the dashboard cannot render.
    uncovered_by_family = {}
    for label, _item, value, _quantity, _unit in recon.get("rows") or []:
        uncovered_by_family[label] = uncovered_by_family.get(label, 0.0) + value

    other = uncovered_by_family.get("other", 0.0)
    if uncovered > 0 and other / uncovered > 0.5:
        return ("unclassified-line-items",
                "%s, and most of it is on line items this script could not "
                "classify. Read the raw line_item strings before assuming which "
                "endpoint explains them." % money)

    biggest = max(uncovered_by_family.items(), key=lambda kv: kv[1],
                  default=("nothing", 0.0))
    return ("gap",
            "%s. Largest uncovered family is %s at $%.2f."
            % (money, biggest[0], biggest[1]))


def hidden_token_types(result):
    """Non-zero audio and image token counts inside a completions result. Pure.

    Returns a sorted list of (field, value). A dashboard summing input_tokens and
    output_tokens whole is mixing these in with text tokens at the text price.
    """
    out = []
    for field in MIXED_TOKEN_FIELDS:
        try:
            value = int(result.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            out.append((field, value))
    return sorted(out)


def get(session, path, params=None):
    r = session.get(API + path, params=params or {}, timeout=60)
    if r.status_code == 401:
        raise SystemExit("401 from OpenAI: OPENAI_ADMIN_KEY must be an "
                         "organization admin key, not a project key")
    if r.status_code == 403:
        raise SystemExit("403 from OpenAI: the key is not authorised for "
                         "/v1/organization")
    r.raise_for_status()
    return r.json()


def cost_items(session, start_time):
    """[(line_item, amount, quantity, quantity_unit), ...] over the window."""
    out = []
    page = get(session, "/organization/costs",
               {"start_time": start_time, "limit": 31, "group_by": "line_item"})
    for bucket in page.get("data") or []:
        for result in bucket.get("results") or []:
            out.append((result.get("line_item"),
                        (result.get("amount") or {}).get("value"),
                        result.get("quantity"),
                        result.get("quantity_unit")))
    return out


def surface_volume(session, path, field, start_time, days):
    """Sum one usage surface's own quantity field over the window."""
    total = 0
    page = get(session, path,
               {"start_time": start_time, "bucket_width": "1d", "limit": days})
    for bucket in page.get("data") or []:
        for result in bucket.get("results") or []:
            try:
                total += int(result.get(field) or 0)
            except (TypeError, ValueError):
                pass
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="days to reconcile (default 30)")
    ap.add_argument("--covers", default="text",
                    help="comma separated families your dashboard renders "
                         "(default 'text', which is what a completions-only "
                         "dashboard covers)")
    ap.add_argument("--tolerance", type=float, default=0.02,
                    help="uncovered share below which the gap is rounding")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_ADMIN_KEY")
    if not key:
        log.error("set OPENAI_ADMIN_KEY (an organization admin key with read scopes)")
        return 2

    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + key})

    now = dt.datetime.now(dt.timezone.utc)
    start = int((now - dt.timedelta(days=args.days)).timestamp())
    covers = [c for c in args.covers.split(",") if c.strip()]

    recon = reconcile(cost_items(session, start), covers)
    state, detail = verdict(recon, args.tolerance)
    log.info("%-24s %s", state, detail)

    for label, line_item, value, quantity, unit in recon["rows"][:20]:
        log.warning("  uncovered  %-10s $%9.2f   %-28s %s %s",
                    label, value, line_item, quantity or "", unit or "")

    for name, path, field, unit in SURFACES:
        volume = surface_volume(session, path, field, start, args.days)
        if volume:
            log.info("  volume     %-28s %s %s", name, volume, unit)

    if recon["unreadable"]:
        log.warning("  %d cost row(s) had an unreadable amount and were left out "
                    "of both sides", recon["unreadable"])

    if state in FINDINGS:
        log.warning("  repair: drive the spend dashboard from "
                    "/v1/organization/costs grouped by line_item, which is the "
                    "only endpoint denominated in money, and use the "
                    "per-modality usage endpoints to explain why a line moved")
        log.warning("  repair: inside completions, read input_text_tokens, "
                    "input_audio_tokens and input_image_tokens separately "
                    "instead of summing input_tokens whole")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
