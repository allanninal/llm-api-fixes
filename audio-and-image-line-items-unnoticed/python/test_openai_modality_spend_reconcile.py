from openai_modality_spend_reconcile import (family, hidden_token_types,
                                             reconcile, verdict)


def items(*rows):
    """[(line_item, amount, quantity, quantity_unit), ...] from the cost report."""
    return [(r[0], r[1], r[2] if len(r) > 2 else None,
             r[3] if len(r) > 3 else None) for r in rows]


def test_the_dashboard_covers_text_and_the_bill_does_not_stop_there():
    recon = reconcile(items(("gpt-5, input tokens", 9000.00),
                            ("gpt-5, output tokens", 6487.43),
                            ("Text-to-speech", 1802.40, 14209881, "characters"),
                            ("Web search", 784.00, 78400, "requests"),
                            ("Image generation", 328.28, 6120, "images")),
                      covers=["text"])
    state, detail = verdict(recon)
    assert state == "gap"
    assert "$18402.11 total" in detail
    assert "$2914.68" in detail
    assert recon["rows"][0][0] == "audio"


def test_model_names_that_look_like_text_but_are_not():
    # Both contain "gpt-", and matching text first would move real money into
    # the covered column and shrink the gap to nothing.
    assert family("gpt-image-1") == "image"
    assert family("gpt-4o-audio-preview, input tokens") == "audio"
    assert family("gpt-5, input tokens") == "text"
    assert family("Code interpreter session") == "tool"
    assert family("text-embedding-3-small") == "embedding"


def test_a_small_gap_is_rounding_and_a_large_one_is_not():
    small = reconcile(items(("gpt-5, input tokens", 1000.00),
                            ("Moderations", 5.00)), covers=["text"])
    assert verdict(small)[0] == "reconciled"
    assert verdict(small, tolerance=0.001)[0] == "gap"


def test_line_items_nobody_can_classify_are_their_own_state():
    recon = reconcile(items(("gpt-5, input tokens", 500.00),
                            ("Some New Surface We Shipped Tuesday", 400.00)),
                      covers=["text"])
    state, detail = verdict(recon)
    assert state == "unclassified-line-items"
    assert "read the raw line_item strings" in detail.lower()


def test_an_unreadable_amount_is_not_counted_as_zero():
    recon = reconcile(items(("gpt-5, input tokens", 100.00),
                            ("Text-to-speech", None),
                            ("Web search", "n/a")), covers=["text"])
    assert recon["unreadable"] == 2
    assert recon["total"] == 100.00
    assert verdict(recon)[0] == "reconciled"


def test_nothing_to_reconcile_is_not_a_finding():
    assert verdict(reconcile([], covers=["text"]))[0] == "no-spend"


def test_multimodal_tokens_hide_inside_the_completions_result():
    result = {"input_tokens": 100000, "output_tokens": 8000,
              "input_text_tokens": 60000, "input_audio_tokens": 40000,
              "output_audio_tokens": 3000, "input_image_tokens": 0}
    assert hidden_token_types(result) == [("input_audio_tokens", 40000),
                                          ("output_audio_tokens", 3000)]
    assert hidden_token_types({"input_tokens": 100000}) == []
