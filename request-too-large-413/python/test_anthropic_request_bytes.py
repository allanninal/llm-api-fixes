from anthropic_request_bytes import (b64_decoded_size, b64_encoded_size,
                                      base64_blobs, content_cap, content_units,
                                      content_verdict, escaping_penalty, human,
                                      inline_budget, probe_state,
                                      serialized_bytes, size_verdict)

MB = 1024 * 1024


def test_a_24mb_file_lands_exactly_on_the_32mb_line():
    # The arithmetic the note is about. Three bytes become four characters, so
    # 24 MiB encodes to precisely 32 MiB and everything else in the request is
    # what takes it over.
    assert b64_encoded_size(24 * MB) == 32 * MB == 33_554_432
    assert size_verdict("messages", 32 * MB)[0] == "near-byte-ceiling"
    state, detail = size_verdict("messages", 32 * MB + 4_096)
    assert state == "over-byte-ceiling"
    assert "Cloudflare" in detail
    assert "never appears in any usage report" in detail
    # And the number to put on the ticket, once the envelope is accounted for.
    assert inline_budget(32 * MB, 4_096) == 24 * MB - 3_072


def test_the_image_cap_is_a_separate_ceiling_from_the_bytes():
    # 300 pages of tiny scans: nowhere near 32 MB, refused anyway, and the cap
    # depends on the model's window rather than on the payload.
    assert content_cap(200_000) == 100
    assert content_cap(1_000_000) == 600
    assert content_cap(None) is None
    assert content_verdict(300, 100)[0] == "over-content-cap"
    assert content_verdict(300, 600)[0] == "content-fits"
    assert content_verdict(300, None)[0] == "content-cap-unknown"
    assert size_verdict("messages", 2 * MB)[0] == "fits"


def test_the_ceiling_depends_on_the_endpoint_not_on_the_body():
    body_size = 200 * MB
    assert size_verdict("messages", body_size)[0] == "over-byte-ceiling"
    assert size_verdict("batches", body_size)[0] == "fits"
    assert size_verdict("files", body_size)[0] == "fits"
    assert size_verdict("responses", body_size)[0] == "endpoint-unknown"


def test_blobs_are_sized_without_decoding_them():
    data = "QUJDREVGR0g="  # eight raw bytes, twelve encoded characters
    body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "read this"},
        {"type": "document", "source": {"type": "base64",
                                        "media_type": "application/pdf",
                                        "data": data}},
    ]}]}
    blobs = base64_blobs(body)
    assert len(blobs) == 1
    assert blobs[0]["media_type"] == "application/pdf"
    assert blobs[0]["encoded"] == 12
    assert blobs[0]["raw"] == b64_decoded_size(data) == 8
    assert blobs[0]["newlines"] is False
    assert content_units(body) == 1


def test_line_wrapped_base64_is_its_own_rejection():
    # Not a size problem at all: several encoders wrap at 76 characters by
    # default and the API will not accept the result.
    body = {"messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "QUJDREVG\nR0g="}}]}]}
    assert base64_blobs(body)[0]["newlines"] is True
    # And the whitespace is not counted as payload when the size is worked out.
    assert base64_blobs(body)[0]["raw"] == 8


def test_a_client_that_escapes_non_ascii_sends_more_than_you_measured():
    body = {"messages": [{"role": "user", "content": "\u3053\u3093\u306b\u3061\u306f" * 100}]}
    plain = serialized_bytes(body)
    escaped = serialized_bytes(body, escape_non_ascii=True)
    assert escaped > plain
    assert escaping_penalty(body) == escaped / float(plain)
    assert escaping_penalty(body) > 1.9
    # ASCII payloads are unaffected, so this never fires as noise.
    assert escaping_penalty({"messages": [{"role": "user", "content": "hello"}]}) == 1.0


def test_the_probe_is_read_as_a_status_code_not_as_a_token_count():
    assert probe_state(413)[0] == "confirmed-413"
    assert probe_state(200)[0] == "under-byte-ceiling"
    assert probe_state(400)[0] == "probe-inconclusive"
    assert probe_state(429)[0] == "probe-inconclusive"


def test_sizes_are_printed_in_binary_units():
    assert human(0) == "0 B"
    assert human(1023) == "1023 B"
    assert human(1024) == "1.0 KB"
    assert human(32 * MB) == "32.0 MB"
