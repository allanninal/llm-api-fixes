"""Compare each configured max_tokens against the model's own published cap.

Read only. GET requests and nothing else: give this a workspace API key. No
payload is ever sent, no tokens are counted, and /v1/messages is never called.
The repair is printed, because choosing an output budget is a judgement about
your product and not a side effect of an audit.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_max_tokens_cap")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# The Batch API raises the output ceiling on the 1M-context models, and only
# behind this header. A batch path that does not send it is capped exactly as a
# synchronous one, which is why the header is an input to the check rather than
# an assumption.
BATCH_300K_BETA = "output-300k-2026-03-24"
BATCH_MAX_TOKENS = 300_000
LONG_CONTEXT_WINDOW = 1_000_000

FINDINGS = ("above-cap", "below-minimum", "cap-unknown", "model-not-found")


def parse_path(spec):
    """Read a NAME=MODEL:MAX_TOKENS argument. Pure. (name, entry) or None."""
    text = str(spec or "").strip()
    if "=" not in text:
        return None
    name, rest = text.split("=", 1)
    if ":" not in rest:
        return None
    model, value = rest.rsplit(":", 1)
    try:
        configured = int(value)
    except (TypeError, ValueError):
        return None
    name, model = name.strip(), model.strip()
    if not name or not model:
        return None
    return (name, {"model": model, "max_tokens": configured, "endpoint": "messages"})


def sync_cap(model_obj):
    """The model object's own max_tokens field. Pure. None if absent.

    This is the source of truth. The published table lags a release and a
    constant in your source lags the table, so a missing field is reported as
    missing rather than filled in from either.
    """
    if not isinstance(model_obj, dict):
        return None
    value = model_obj.get("max_tokens")
    return value if isinstance(value, int) and value > 0 else None


def window_of(model_obj):
    """max_input_tokens off a model object. Pure. Used only to size the batch
    ceiling, which applies to the 1M-context models."""
    if not isinstance(model_obj, dict):
        return None
    value = model_obj.get("max_input_tokens")
    return value if isinstance(value, int) and value > 0 else None


def effective_cap(model_obj, endpoint="messages", betas=()):
    """The legal ceiling for max_tokens on one model at one endpoint. Pure.

    Two inputs, because the ceiling belongs to the pair and not to the model.
    A batch path with the output-300k header on a 1M-context model gets the
    higher number; the same path without the header does not, and neither does
    a 200k-context model that has it.
    """
    cap = sync_cap(model_obj)
    if cap is None:
        return (None, "the model object carried no max_tokens field")
    if str(endpoint) == "batches" and BATCH_300K_BETA in set(betas or ()):
        window = window_of(model_obj)
        if window is not None and window >= LONG_CONTEXT_WINDOW:
            return (BATCH_MAX_TOKENS, "the Batch API with " + BATCH_300K_BETA)
        return (cap, "the model object; the 300K batch ceiling needs a "
                     "1M context model")
    return (cap, "the model object")


def verdict(configured, cap):
    """Classify one configured value against one cap. Pure. (state, detail)."""
    configured = int(configured or 0)
    if configured < 1:
        return ("below-minimum",
                "max_tokens is %d, and the minimum accepted value is 1"
                % configured)
    if cap is None:
        return ("cap-unknown",
                "max_tokens is %d and no ceiling could be read for this model "
                "and endpoint" % configured)
    if configured > cap:
        return ("above-cap",
                "max_tokens is %d against a cap of %d, which is a 400 "
                "invalid_request_error on every call, %d over"
                % (configured, cap, configured - cap))
    if configured == cap:
        return ("at-cap",
                "max_tokens is %d, exactly the cap, so any move to a smaller "
                "model breaks this path" % configured)
    return ("within-cap",
            "max_tokens is %d of a %d cap (%.0f%%)"
            % (configured, cap, configured / float(cap) * 100))


def tier_spans(rows):
    """One configured value reused across models with different ceilings. Pure.

    rows: [(name, model_id, configured, cap)]. Returns [(value, [model ids])].

    The number appears once in the source, so nothing at any call site says
    that its effective ceiling is the smallest cap among the models using it.
    That is the finding even on the day every path still passes, because the
    next model swap is the one that turns it into a 400.
    """
    by_value = {}
    for name, model, configured, cap in rows or []:
        by_value.setdefault(int(configured or 0), []).append((name, model, cap))
    out = []
    for value in sorted(by_value):
        entries = by_value[value]
        models = sorted({m for _n, m, _c in entries})
        if len(models) < 2:
            continue
        out.append((value, models))
    return out


def get_model(session, model_id):
    """One GET per distinct model id. A 404 here belongs to a different note."""
    r = session.get(API + "/models/" + str(model_id), timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: ANTHROPIC_API_KEY has to be a "
                         "workspace key" % r.status_code)
    r.raise_for_status()
    return r.json()


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="JSON file of call paths: "
                                     '{"name": {"model": ..., "max_tokens": ..., '
                                     '"endpoint": "messages|batches", "betas": []}}')
    ap.add_argument("--path", action="append", default=[], metavar="NAME=MODEL:MAX",
                    help="one call path in shorthand, repeatable")
    ap.add_argument("--show-all", action="store_true",
                    help="also print paths comfortably under their cap")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a workspace key")
        return 2

    paths = dict(load_config(args.config)) if args.config else {}
    for spec in args.path:
        parsed = parse_path(spec)
        if parsed is None:
            log.error("cannot read --path %r, expected NAME=MODEL:MAX_TOKENS", spec)
            return 2
        paths[parsed[0]] = parsed[1]
    if not paths:
        log.error("give --config FILE or at least one --path NAME=MODEL:MAX_TOKENS")
        return 2

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION})

    models = {}
    rows = []
    bad = 0

    for name in sorted(paths):
        entry = paths[name] or {}
        model_id = str(entry.get("model") or "")
        configured = entry.get("max_tokens")
        endpoint = entry.get("endpoint") or "messages"
        betas = entry.get("betas") or []

        if model_id not in models:
            models[model_id] = get_model(session, model_id)
        model_obj = models[model_id]
        if model_obj is None:
            bad += 1
            log.warning("%-14s %-16s %-28s the model id is not in the live list "
                        "at all, which is a retirement or a typo rather than a "
                        "max_tokens problem", "model-not-found", name, model_id)
            continue

        cap, source = effective_cap(model_obj, endpoint, betas)
        state, detail = verdict(configured, cap)
        rows.append((name, model_id, int(configured or 0), cap))

        line = "%-14s %-16s %-28s %s" % (state, name, model_id, detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
            log.warning("  ceiling read from %s", source)
        elif state == "at-cap":
            log.warning(line)
        elif args.show_all:
            log.info(line)

    for value, shared in tier_spans(rows):
        caps = [cap for _n, _m, configured, cap in rows
                if configured == value and cap is not None]
        note = "shared value %d is configured on %d model(s): %s" % (
            value, len(shared), ", ".join(shared))
        if caps and min(caps) < value:
            bad += 1
            log.warning("%-14s %s, and the smallest cap among them is %d",
                        "spans-tiers", note, min(caps))
        else:
            log.info("  %s, so the effective ceiling is the smallest of their "
                     "caps whether or not anything says so", note)

    if bad:
        log.warning("  repair: set each path's max_tokens from the cap the "
                    "Models API reports for its own model, not from a shared "
                    "constant and not from the docs table, which lags. Note "
                    "that maxing it out trades a 400 for truncated answers and "
                    "long non-streaming requests. Printed, not applied.")

    log.info("%d path(s) checked, %d finding(s)", len(paths), bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
