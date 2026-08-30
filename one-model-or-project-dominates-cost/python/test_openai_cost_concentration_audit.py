from openai_cost_concentration_audit import rank, unit_price, verdict


def row(name="gpt-5.6-sol, input", amount=0.0, quantity=0.0, unit="tokens"):
    return {"name": name, "amount": amount, "quantity": quantity, "unit": unit}


def result(line_item="gpt-5.6-sol, input", value=0.0, quantity=0.0,
           unit="tokens", project=None):
    return {"line_item": line_item, "project_id": project,
            "amount": {"value": value, "currency": "usd"},
            "quantity": quantity, "quantity_unit": unit}


def bucket(*results):
    return {"start_time": 0, "end_time": 86400, "results": list(results)}


def test_one_row_carrying_most_of_the_bill_is_the_finding():
    state, detail = verdict([row(amount=7800.0), row(name="b", amount=1500.0),
                             row(name="c", amount=700.0)])
    assert state == "dominant"
    assert "78% of $10000.00" in detail
    assert "at most 22% of the bill" in detail


def test_two_large_rows_are_not_one_dominant_row():
    state, detail = verdict([row(name="a", amount=4000.0),
                             row(name="b", amount=3800.0),
                             row(name="c", amount=2200.0)])
    assert state == "top-heavy"
    assert "78% of $10000.00 between them" in detail


def test_a_bill_with_no_lever_in_it_is_an_answer():
    rows = [row(name=str(i), amount=2000.0) for i in range(5)]
    state, detail = verdict(rows)
    assert state == "spread"
    assert "across 5 row(s)" in detail
    assert verdict([])[0] == "no-spend"
    assert verdict([row(amount=0.4)])[0] == "no-spend"


def test_a_null_top_row_is_unattributable_rather_than_unknown():
    state, detail = verdict([row(name=None, amount=9000.0),
                             row(name="b", amount=1000.0)])
    assert state == "unattributable"
    assert "no name" in detail
    assert "Null is not unknown" in detail
    # Below the threshold it is just another row, not a finding.
    assert verdict([row(name="a", amount=6000.0),
                    row(name=None, amount=4000.0)])[0] == "dominant"


def test_the_unit_price_is_only_computed_for_token_units():
    assert unit_price(200.0, 50000000, "tokens") == 4.0
    assert unit_price(200.0, 50000, "1000_tokens") == 4.0
    assert unit_price(20.0, 4, "images") is None
    assert unit_price(20.0, 4, "duration_hours") is None
    assert unit_price(20.0, 0, "tokens") is None
    assert unit_price(20.0, 100, None) is None
    assert unit_price(20.0, 100, "mixed") is None


def test_ranking_sums_across_buckets_and_keeps_a_null_name_null():
    rows = rank([
        bucket(result(value=60.0, quantity=15000000),
               result(line_item="gpt-5.6-luna, input", value=10.0, quantity=1000000)),
        bucket(result(value=30.0, quantity=7500000),
               result(line_item=None, value=5.0, quantity=0, unit=None)),
    ], "line_item")
    assert [r["name"] for r in rows] == ["gpt-5.6-sol, input",
                                         "gpt-5.6-luna, input", None]
    assert rows[0]["amount"] == 90.0
    assert rows[0]["quantity"] == 22500000
    assert rows[0]["share"] == 0.8571
    assert rows[2]["name"] is None
    assert rows[2]["unit"] is None


def test_mixed_units_in_one_row_are_reported_as_mixed_not_guessed():
    rows = rank([bucket(result(value=1.0, quantity=10, unit="tokens"),
                        result(value=1.0, quantity=2, unit="images"))],
                "line_item")
    assert rows[0]["unit"] == "mixed"
    assert unit_price(rows[0]["amount"], rows[0]["quantity"], rows[0]["unit"]) is None
