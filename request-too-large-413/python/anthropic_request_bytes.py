"""Measure a Claude request in bytes against the 32 MB ceiling.

Read only. One GET for the model object, and one optional call to
/v1/messages/count_tokens, which is free, creates no object, generates no
completion and is not billed. That call is used here only as an oracle: it
shares the same 32 MB ceiling, so its status code tells you whether message
creation would refuse the same body, at no cost. Its input_tokens number is
deliberately never read, because this script is about bytes.

/v1/messages is never called and nothing is uploaded. The repair is printed.
"""
import argparse
import json
import logging
import os
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anthropic_request_bytes")

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"

MB = 1024 * 1024

# Per endpoint, in bytes. Binary megabytes: if a payload lands within a percent
# of one of these lines, treat it as over rather than arguing about whether the
# published number meant 1000 or 1024, because the margin is not worth an
# outage.
CEILINGS = {
    "messages": 32 * MB,
    "count_tokens": 32 * MB,
    "batches": 256 * MB,
    "files": 500 * MB,
}

# Sampling parameters the counting endpoint rejects. Stripped only for the
# probe; the measurement is always taken on the real body.
SAMPLING_ONLY = ("max_tokens", "stream", "temperature", "top_p", "top_k",
                 "stop_sequences", "metadata", "service_tier")

NEWLINES = ("\n", "\r")

FINDINGS = ("over-byte-ceiling", "near-byte-ceiling", "over-content-cap",
            "base64-has-newlines")


def serialized_bytes(body, escape_non_ascii=False):
    """The size of the JSON that actually goes on the wire. Pure.

    Measuring the object rather than the string is the mistake this exists to
    stop: a payload can be well inside the ceiling as a dict and outside it as
    the bytes a client sends, which is the only size the proxy in front of the
    API ever sees.
    """
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=escape_non_ascii)
    return len(text.encode("utf-8"))


def human(size):
    """Bytes as a short readable string. Pure. Binary units throughout."""
    n = float(size or 0)
    if n < 1024:
        return "%d B" % int(n)
    if n < MB:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / float(MB))


def b64_encoded_size(raw_bytes):
    """How large a file becomes once base64 encoded. Pure.

    Three bytes in, four characters out, rounded up to the padding boundary.
    Exactly a third larger, which is why a 24 MiB file lands on precisely the
    32 MiB line before a single key of JSON is wrapped around it.
    """
    raw = max(0, int(raw_bytes or 0))
    return ((raw + 2) // 3) * 4


def b64_decoded_size(text):
    """The raw size behind a base64 string, without decoding it. Pure.

    Decoding a 32 MB string to find out how big the original was allocates 24 MB
    to answer a question arithmetic answers for free.
    """
    clean = "".join(str(text or "").split())
    if not clean:
        return 0
    return (len(clean) // 4) * 3 - clean.count("=")


def inline_budget(ceiling, envelope=0):
    """The largest raw file that still fits inline under `ceiling`. Pure.

    The number worth writing on the ticket. Everything above it has to go
    through the Files API whatever anybody hoped.
    """
    room = max(0, int(ceiling or 0) - max(0, int(envelope or 0)))
    return (room // 4) * 3


def content_blocks(body):
    """Every content block in a Messages body, flattened. Pure."""
    out = []
    if not isinstance(body, dict):
        return out
    system = body.get("system")
    if isinstance(system, list):
        out.extend(b for b in system if isinstance(b, dict))
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            out.extend(b for b in content if isinstance(b, dict))
    return out


def content_units(body):
    """Images and documents in one request. Pure.

    Counted against a ceiling that has nothing to do with bytes or tokens: a
    request may carry a limited number of images and PDF pages whatever its
    size, and a scanned document can pass both other checks and fail this one.
    """
    return sum(1 for b in content_blocks(body)
               if b.get("type") in ("image", "document"))


def base64_blobs(body):
    """Every inline base64 attachment, sized. Pure."""
    out = []
    for block in content_blocks(body):
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            continue
        data = source.get("data")
        if not isinstance(data, str):
            continue
        out.append({
            "block": block.get("type"),
            "media_type": source.get("media_type"),
            "encoded": len(data.encode("utf-8")),
            "raw": b64_decoded_size(data),
            "newlines": any(ch in data for ch in NEWLINES),
        })
    return out


def escaping_penalty(body):
    """How much larger the body gets if the client escapes non-ASCII. Pure.

    A JSON encoder writing backslash-u escapes turns one three-byte character
    into six ASCII ones. On a payload that is mostly CJK or emoji that is close
    to a doubling, and it happens after you measured and before the request
    leaves.
    """
    plain = serialized_bytes(body, escape_non_ascii=False)
    if plain <= 0:
        return 1.0
    return serialized_bytes(body, escape_non_ascii=True) / float(plain)


def content_cap(window):
    """Images and PDF pages allowed in one request. Pure. None if unknown.

    Read off the model's context window because the two move together: 100 on
    the 200k-context models, 600 on the larger ones. This is still not a token
    check. The window is being used here only to pick which content cap applies.
    """
    if not isinstance(window, int) or window <= 0:
        return None
    return 100 if window <= 200_000 else 600


def size_verdict(endpoint, size, near=0.8):
    """Classify one serialized body against one endpoint ceiling. Pure."""
    ceiling = CEILINGS.get(endpoint)
    if ceiling is None:
        return ("endpoint-unknown",
                "no published byte ceiling for %r, so there is nothing to "
                "compare %s against" % (endpoint, human(size)))
    shape = "%s of %s (%.0f%%)" % (human(size), human(ceiling),
                                   size / float(ceiling) * 100)
    if size > ceiling:
        return ("over-byte-ceiling",
                "%s. Cloudflare refuses this in front of the API with 413 "
                "request_too_large, so it never reaches Anthropic and never "
                "appears in any usage report." % shape)
    if size >= ceiling * near:
        return ("near-byte-ceiling",
                "%s. Base64 costs a third on the way in, so one more "
                "attachment crosses the line." % shape)
    return ("fits", "%s." % shape)


def content_verdict(units, cap):
    """Classify the image and page count against the per request cap. Pure."""
    if cap is None:
        return ("content-cap-unknown",
                "%d image or document block(s), and no window on the model "
                "object to size the per request cap from" % units)
    if units > cap:
        return ("over-content-cap",
                "%d image or document block(s) against a cap of %d for this "
                "model, which is refused whatever the payload weighs"
                % (units, cap))
    return ("content-fits", "%d image or document block(s) of a %d cap"
            % (units, cap))


def probe_state(status):
    """What the free counting endpoint's status code proves. Pure.

    Status only. The body carries a token count and this script does not read
    it: that number belongs to the context window ceiling, which is a separate
    limit with a separate repair.
    """
    if status == 413:
        return ("confirmed-413",
                "the counting endpoint refused this body at the same 32 MB "
                "ceiling, so message creation refuses it too")
    if status == 200:
        return ("under-byte-ceiling",
                "the counting endpoint accepted the body, so it is inside the "
                "32 MB ceiling for the endpoints that share it")
    return ("probe-inconclusive",
            "the counting endpoint answered %s, which is neither the 413 nor "
            "the 200 this probe reads" % status)


def get(session, path):
    r = session.get(API + path, timeout=30)
    if r.status_code in (401, 403):
        raise SystemExit("%d from Anthropic: ANTHROPIC_API_KEY has to be a "
                         "workspace key" % r.status_code)
    r.raise_for_status()
    return r.json()


def probe(session, body):
    """The one non-GET call, and it neither creates nor bills anything.

    The trimmed body is a few dozen bytes smaller than the one you will send.
    That matters only if you are within a few dozen bytes of 32 MB, and if you
    are, you are over.
    """
    trimmed = {k: v for k, v in (body or {}).items() if k not in SAMPLING_ONLY}
    r = session.post(API + "/messages/count_tokens", json=trimmed, timeout=120)
    return r.status_code


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--payload", action="append", default=[], required=True,
                    metavar="FILE", help="a JSON file holding a real request body")
    ap.add_argument("--endpoint", default="messages",
                    choices=sorted(CEILINGS), help="which ceiling applies")
    ap.add_argument("--near", type=float, default=0.8,
                    help="share of the ceiling at which a body that still fits "
                         "is reported anyway (default 0.8)")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the free count_tokens status check")
    ap.add_argument("--show-all", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("set ANTHROPIC_API_KEY to a workspace key")
        return 2

    session = requests.Session()
    session.headers.update({"x-api-key": key, "anthropic-version": VERSION,
                            "content-type": "application/json"})

    windows = {}
    checked = 0
    bad = 0

    for path in args.payload:
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
        checked += 1

        size = serialized_bytes(body)
        state, detail = size_verdict(args.endpoint, size, args.near)
        line = "%-20s %-30s %s" % (state, path, detail)
        if state in FINDINGS:
            bad += 1
            log.warning(line)
        elif state == "endpoint-unknown":
            log.warning(line)
        elif args.show_all:
            log.info(line)

        blobs = base64_blobs(body)
        if blobs:
            raw = sum(b["raw"] for b in blobs)
            encoded = sum(b["encoded"] for b in blobs)
            log.info("  base64: %d blob(s), %s raw inflated to %s encoded (%.0f%%)",
                     len(blobs), human(raw), human(encoded),
                     encoded / float(raw) * 100 if raw else 0)
        broken = [b for b in blobs if b["newlines"]]
        if broken:
            bad += 1
            log.warning("%-20s %-30s %d inline blob(s) contain line breaks; "
                        "inline base64 has to be unbroken, and several encoders "
                        "still wrap at 76 characters by default",
                        "base64-has-newlines", path, len(broken))

        penalty = escaping_penalty(body)
        if penalty > 1.05:
            log.warning("  a client escaping non-ASCII would send %.0f%% more "
                        "than measured here (%s), which is enough to cross the "
                        "ceiling on its own",
                        (penalty - 1) * 100, human(int(size * penalty)))

        model = str(body.get("model") or "")
        window = None
        if model:
            if model not in windows:
                obj = get(session, "/models/" + model)
                windows[model] = obj.get("max_input_tokens")
            window = windows[model]
        units = content_units(body)
        if units:
            cstate, cdetail = content_verdict(units, content_cap(window))
            if cstate == "over-content-cap":
                bad += 1
                log.warning("%-20s %-30s %s", cstate, path, cdetail)
            elif cstate == "content-cap-unknown":
                log.warning("%-20s %-30s %s", cstate, path, cdetail)
            elif args.show_all:
                log.info("%-20s %-30s %s", cstate, path, cdetail)

        if not args.no_probe:
            pstate, pdetail = probe_state(probe(session, body))
            log.info("  probe: %s, %s", pstate, pdetail)

        if state in ("over-byte-ceiling", "near-byte-ceiling"):
            ceiling = CEILINGS[args.endpoint]
            envelope = size - sum(b["encoded"] for b in blobs)
            log.warning("  largest raw file that still fits inline on this "
                        "endpoint: %s", human(inline_budget(ceiling, envelope)))
            log.warning("  repair: upload the attachment once through the Files "
                        "API (500 MB) and reference it by file_id, which takes "
                        "the bytes out of every request rather than one. Or "
                        "split the request. Printed, not performed.")

    log.info("%d payload(s) checked, %d finding(s)", checked, bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
