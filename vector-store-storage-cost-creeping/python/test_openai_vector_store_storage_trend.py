from openai_vector_store_storage_trend import (GIB, UNGROUPED, byte_series,
                                                growth, idle_stores,
                                                query_series, repair_lines,
                                                searches_by_store, slope,
                                                storage_lines, verdict,
                                                window_start)

DAY = 86400
T0 = 1_800_000_000


def series(first, last, points=90, key="usage_bytes", project="proj_research"):
    """A straight line from first to last, as usage buckets."""
    out = []
    for i in range(points):
        value = first + (last - first) * i // max(points - 1, 1)
        out.append({"start_time": T0 + i * DAY,
                    "results": [{"object": "organization.usage.vector_stores.result",
                                 key: value, "project_id": project}]})
    return out


def test_bytes_tripling_with_no_queries_at_all_is_the_finding():
    # The note. Nothing is wrong with the index; the money is being spent on
    # holding it rather than on using it.
    points = byte_series(series(int(8.1 * GIB), int(31.4 * GIB)))["proj_research"]
    state, detail = verdict(points, [], 90)
    assert state == "bytes-growing-never-queried"
    assert "8.1 GiB -> 31.4 GiB" in detail and "+288%" in detail
    idle = idle_stores(
        [{"id": "vs_c3", "name": "march-demo", "usage_bytes": int(12.4 * GIB),
          "last_active_at": T0 - 148 * DAY}],
        {"vs_c3": 0}, T0)
    lines = repair_lines(state, idle)
    assert any("march-demo" in line and "12.4 GiB" in line for line in lines)
    assert any("expiration policy at creation" in line for line in lines)


def test_the_same_growth_with_rising_queries_is_not_a_finding():
    # The reading this note must not trample. A corpus that is growing because
    # it is being used more is supposed to cost more.
    points = byte_series(series(int(44 * GIB), int(61 * GIB)))["proj_research"]
    queries = query_series(series(400, 14_000, key="num_requests"))["proj_research"]
    state, detail = verdict(points, queries, 90)
    assert state == "bytes-and-queries-growing"
    assert "Growth, priced correctly" in detail
    assert repair_lines(state)[0].startswith("nothing to do")


def test_the_size_floor_comes_before_the_growth_rate():
    tiny = byte_series(series(int(0.02 * GIB), int(0.12 * GIB)))["proj_research"]
    state, detail = verdict(tiny, [], 90)
    assert state == "below-threshold"
    assert "0.1 GiB" in detail
    assert repair_lines(state) == []


def test_storage_is_selected_by_unit_and_never_by_name():
    buckets = [{"results": [
        {"line_item": "Vector store storage", "quantity_unit": "gibibyte_hours",
         "quantity": 41_288.0, "amount": {"value": 412.88, "currency": "usd"}},
        {"line_item": "gpt-5, input", "quantity_unit": "tokens",
         "quantity": 9_000_000, "amount": {"value": 18_402.11, "currency": "usd"}},
        {"line_item": "Storage, renamed next quarter",
         "quantity_unit": "gibibyte_hours", "quantity": 10.0,
         "amount": {"value": 0.1, "currency": "usd"}}]}]
    lines = storage_lines(buckets)
    assert set(lines) == {"Vector store storage", "Storage, renamed next quarter"}
    assert round(sum(v["dollars"] for v in lines.values()), 2) == 412.98
    assert storage_lines([]) == {}


def test_an_unattributed_row_never_becomes_a_store():
    buckets = [{"results": [
        {"num_requests": 12, "vector_store_id": "vs_a1", "project_id": "proj_a"},
        {"num_requests": 3, "vector_store_id": None, "project_id": None}]}]
    per_store = searches_by_store(buckets)
    assert per_store == {"vs_a1": 12, UNGROUPED: 3}
    assert byte_series([{"start_time": T0, "results": [
        {"usage_bytes": 5, "project_id": None}]}]) == {UNGROUPED: [(T0, 5)]}


def test_idle_stores_need_real_bytes_and_zero_searches():
    stores = [
        {"id": "vs_big", "name": "corpus", "usage_bytes": int(9 * GIB),
         "last_active_at": T0 - 96 * DAY},
        {"id": "vs_busy", "name": "live", "usage_bytes": int(9 * GIB),
         "last_active_at": T0},
        {"id": "vs_small", "name": "scratch", "usage_bytes": 40 * 1024 * 1024,
         "last_active_at": T0 - 400 * DAY},
        {"id": "vs_never", "name": "no-timestamp", "usage_bytes": int(2 * GIB),
         "last_active_at": None}]
    rows = idle_stores(stores, {"vs_busy": 900}, T0)
    assert [r[0] for r in rows] == ["vs_big", "vs_never"]
    assert rows[0][3] == 96
    assert rows[1][3] == -1
    assert idle_stores(None, None, T0) == []


def test_the_slope_is_zero_on_a_flat_series_and_on_one_point():
    flat = [(T0 + i * DAY, 1000) for i in range(30)]
    assert slope(flat) == 0.0
    assert slope([(T0, 5)]) == 0.0
    assert slope([]) == 0.0
    rising = [(T0 + i * DAY, 100 * i) for i in range(10)]
    assert round(slope(rising), 3) == 100.0
    assert growth([]) == (0, 0, 0, 0.0)
    assert growth([(T0, 0), (T0 + DAY, 50)])[3] == 0.0


def test_the_window_starts_at_midnight_utc():
    import datetime as dt
    now = dt.datetime(2026, 8, 31, 17, 45, 12, tzinfo=dt.timezone.utc)
    assert window_start(90, now) == int(
        dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc).timestamp())
