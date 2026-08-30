"""Estimate whether a non-streaming Claude call can finish inside 10 minutes.

Read only, with one deliberate exception. Nothing here creates a completion:
where a call path names a payload file, that body goes to
/v1/messages/count_tokens, which is free, creates no object, generates no
output and is not billed. It is used to turn the input into prefill seconds.
Everything else is a GET, and /v1/messages is never called.

The repair is a transport change and it is printed. Moving a production call
path onto streaming changes error handling and back pressure, which is a
decision, not an audit's side effect.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_wall_clock_preflight")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

# The documented ceiling for a single non-streaming Messages request.
WALL_CLOCK = 600.0

# Starting figures, both meant to be replaced with your own measurements.
# Generation is the one that decides the answer; prefill is fast enough that it
# only matters on very large inputs, and proving that is half the point.
DEFAULT_TPS = 55.0
DEFAULT_PREFILL_TPS = 6000.0

# Client timeouts are not expressed in the same unit across SDKs, and a number
# copied from one language's example into another's constructor is the quiet
# half of this note.
SDK_TIMEOUT_UNITS = {
    "python": ("seconds", 1.0),
    "ruby": ("seconds", 1.0),
    "php": ("seconds", 1.0),
    "typescript": ("milliseconds", 0.001),
    "javascript": ("milliseconds", 0.001),
    "node": ("milliseconds", 0.001),
    "go": ("a time.Duration", 1.0),
    "java": ("a Duration", 1.0),
    "csharp": ("a TimeSpan", 1.0),
}
MILLISECOND_SDKS = ("typescript", "javascript", "node")

SAMPLING_ONLY = ("max_tokens", "stream", "temperature", "top_p", "top_k",
                 "stop_sequences", "metadata", "service_tier")

FINDINGS = ("over-wall-clock-not-streaming", "over-client-timeout",
            "near-wall-clock-not-streaming")


def duration(seconds):
    """Seconds as minutes and seconds. Pure."""
    total = int(max(0.0, float(seconds or 0)))
    return "%dm %02ds" % (total // 60, total % 60)


def generation_seconds(max_tokens, tps=DEFAULT_TPS):
    """How long it takes to write max_tokens output tokens. Pure.

    This is the number the whole note turns on, and it has nothing to do with
    the size of the prompt. Thinking tokens are output tokens, so an effort
    setting moves it too.
    """
    rate = float(tps or 0)
    if rate <= 0:
        return 0.0
    return max(0, int(max_tokens or 0)) / rate


def prefill_seconds(input_tokens, prefill_tps=DEFAULT_PREFILL_TPS):
    """How long it takes to read the input. Pure.

    Kept separate and reported separately because it is almost always small,
    and the point of measuring it is to stop people shortening prompts to fix a
    problem that lives entirely on the output side.
    """
    rate = float(prefill_tps or 0)
    if rate <= 0:
        return 0.0
    return max(0, int(input_tokens or 0)) / rate


def timeout_seconds(sdk, value):
    """A client timeout in seconds, whatever unit the SDK takes. Pure.

    None when the SDK is unknown, because guessing the unit is precisely the
    mistake this function exists to catch.
    """
    if value is None:
        return None
    unit = SDK_TIMEOUT_UNITS.get(str(sdk or "").strip().lower())
    if unit is None:
        return None
    try:
        return float(value) * unit[1]
    except (TypeError, ValueError):
        return None


def unit_suspicion(sdk, value):
    """True when a timeout looks written in the wrong unit. Pure.

    600 in the TypeScript client is six hundred milliseconds, not ten minutes.
    Nobody chooses a sub-second timeout for an LLM call on purpose, so anything
    under a second on a millisecond SDK is a number copied from a seconds-based
    example.
    """
    seconds = timeout_seconds(sdk, value)
    if seconds is None:
        return False
    return str(sdk or "").strip().lower() in MILLISECOND_SDKS and seconds < 1.0


def safe_max_tokens(tps=DEFAULT_TPS, wall_clock=WALL_CLOCK, prefill=0.0):
    """The largest max_tokens that still finishes inside the ceiling. Pure.

    The number to put in the config, as opposed to the model's cap, which is
    the number that fits in the request.
    """
    rate = float(tps or 0)
    room = max(0.0, float(wall_clock or 0) - max(0.0, float(prefill or 0)))
    if rate <= 0:
        return 0
    return int(room * rate)


def verdict(seconds, streams, timeout_s=None, wall_clock=WALL_CLOCK, near=0.8):
    """Classify one call path against the clock. Pure. (state, detail).

    Order matters. The wall clock is checked before the client timeout, because
    a non-streaming request past ten minutes fails on the far side whatever the
    client is configured to wait for, and raising the client timeout is both
    the first repair people reach for and the one that does nothing.
    """
    shape = "%s of generation estimated" % duration(seconds)

    if not streams and seconds > wall_clock:
        return ("over-wall-clock-not-streaming",
                "%s on a non-streaming path, past the %s ceiling. That is a 504 "
                "timeout_error, or no response at all when an intermediate hop "
                "drops the idle connection first. Raising the client timeout "
                "does not move it." % (shape, duration(wall_clock)))
    if timeout_s is not None and seconds > timeout_s:
        return ("over-client-timeout",
                "%s against a client timeout of %s, so the client gives up "
                "before the API is finished." % (shape, duration(timeout_s)))
    if not streams and seconds >= wall_clock * near:
        return ("near-wall-clock-not-streaming",
                "%s on a non-streaming path, inside %.0f%% of the %s ceiling. "
                "One unusually long answer crosses it."
                % (shape, near * 100, duration(wall_clock)))
    if streams and seconds > wall_clock:
        return ("streams-past-ten-minutes",
                "%s, and the path streams, so the connection never goes idle "
                "and the ceiling does not apply. Worth the Message Batches API "
                "if nobody is waiting on it." % shape)
    return ("within-budget", "%s." % shape)


def get(session, path):
    r = session.get(API + path, timeout=30)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: ANTHROPIC_API_KEY has to be a "
                         "workspace key" % r.status_code)
    r.raise_for_status()
    return r.json()


def count_input(session, payload_path):
    """The one non-GET call, and it neither creates nor bills anything.

    The counting endpoint returns an input_tokens number for free. Here that
    number is converted straight into seconds of prefill; it is not compared
    against any ceiling, which is a different note.
    """
    with open(payload_path, "r", encoding="utf-8") as fh:
        body = json.load(fh)
    trimmed = {k: v for k, v in body.items() if k not in SAMPLING_ONLY}
    r = session.post(API + "/messages/count_tokens", json=trimmed, timeout=60)
    r.raise_for_status()
    return int((r.json() or {}).get("input_tokens") or 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True,
                    help="JSON file of call paths: "
                         '{"name": {"model": ..., "max_tokens": ..., '
                         '"stream": false, "sdk": "typescript", '
                         '"timeout": 600, "payload": "body.json"}}')
    ap.add_argument("--tps", type=float, default=DEFAULT_TPS,
                    help="observed output tokens per second (default 55)")
    ap.add_argument("--prefill-tps", type=float, default=DEFAULT_PREFILL_TPS,
                    help="observed input tokens per second (default 6000)")
    ap.add_argument("--show-all", action="store_true",
                    help="also print paths comfortably inside the clock")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a workspace key")
        return 2

    with open(args.config, "r", encoding="utf-8") as fh:
        paths = json.load(fh)

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION,
                            "content-type": "application/json"})

    caps = {}
    bad = 0

    for name in sorted(paths):
        entry = paths[name] or {}
        model_id = str(entry.get("model") or "")
        streams = bool(entry.get("stream"))
        sdk = entry.get("sdk")

        if model_id and model_id not in caps:
            caps[model_id] = get(session, "/models/" + model_id).get("max_tokens")

        input_tokens = int(entry.get("input_tokens") or 0)
        if entry.get("payload"):
            input_tokens = count_input(session, entry["payload"])

        prefill = prefill_seconds(input_tokens, args.prefill_tps)
        seconds = prefill + generation_seconds(entry.get("max_tokens"), args.tps)
        client = timeout_seconds(sdk, entry.get("timeout"))

        state, detail = verdict(seconds, streams, client)
        line = "%-30s %-16s %s" % (state, name, detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
        elif state == "streams-past-ten-minutes":
            log.info(line)
        elif args.show_all:
            log.info(line)

        if unit_suspicion(sdk, entry.get("timeout")):
            bad += 1
            log.warning("%-30s %-16s timeout %s on the %s client is %.1fs, not "
                        "%s: that unit is milliseconds",
                        "timeout-unit-mistake", name, entry.get("timeout"), sdk,
                        client or 0.0, duration(entry.get("timeout") or 0))

        if state in ("over-wall-clock-not-streaming",
                     "near-wall-clock-not-streaming"):
            log.warning("  at %.0f tok/s the largest max_tokens that finishes "
                        "inside the ceiling is %d",
                        args.tps, safe_max_tokens(args.tps, WALL_CLOCK, prefill))
            cap = caps.get(model_id)
            if cap:
                log.warning("  this model allows %d output tokens, which is %s "
                            "on one call", cap,
                            duration(generation_seconds(cap, args.tps)))
            log.warning("  repair: stream it. .stream() plus "
                        ".get_final_message() returns the identical Message "
                        "object with no event handling, and the connection "
                        "never goes idle. For latency tolerant work use the "
                        "Message Batches API, which has no such clock. Printed, "
                        "not applied.")

    log.info("%d path(s) checked, %d finding(s)", len(paths), bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
