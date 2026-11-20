"""Cross-device merge: asset continuity across device replacement (换机)."""
from app import db, merge, pipeline

from conftest import activate, add_device, make_rules, series

H = 3600


def _setup_asset(conn, d1_hole: tuple[int, int] | None = None):
    add_device(conn, "D1")
    add_device(conn, "D2")
    activate(conn, make_rules())
    cut = 12 * H
    conn.executemany(
        "INSERT INTO asset_links(asset_id, device_id, valid_from, valid_to) VALUES (?,?,?,?)",
        [("A1", "D1", 0, cut), ("A1", "D2", cut, None)])
    conn.commit()
    # D1 overruns the cutover by 1h; D2 starts 1h early (overlap period)
    d1 = series("D1", 0, 13 * H)
    if d1_hole:
        d1 = [p for p in d1 if not (d1_hole[0] <= p["ts"] < d1_hole[1])]
    pipeline.ingest_points(conn, d1, "t", "live", 13 * H + 5)
    pipeline.ingest_points(conn, series("D2", 11 * H, 24 * H), "t", "live", 24 * H + 5)
    return cut


def test_merged_series_respects_validity(conn):
    cut = _setup_asset(conn)
    pts = merge.merged_series(conn, "A1", "vibration", 0, 24 * H)
    assert pts, "merged series empty"
    # every point attributed to the device valid at that moment
    assert all(p["device_id"] == "D1" for p in pts if p["ts"] < cut)
    assert all(p["device_id"] == "D2" for p in pts if p["ts"] >= cut)
    # no double-counting: each ts appears exactly once
    tss = [p["ts"] for p in pts]
    assert len(tss) == len(set(tss))
    # continuity across the cutover: grid stays complete
    assert tss == list(range(0, 24 * H, 60))


def test_merged_health_timeline(conn):
    cut = _setup_asset(conn)
    tl = merge.merged_health(conn, "A1", 0, 24 * H)
    assert tl, "merged health empty"
    by_device = {}
    for row in tl:
        by_device.setdefault(row["device_id"], []).append(row["window_start"])
    assert max(by_device["D1"]) < cut <= min(by_device["D2"])
    # windows around the cutover both present, no overlap window
    assert 11 * H in by_device["D1"] and 12 * H in by_device["D2"]
    starts = [r["window_start"] for r in tl]
    assert len(starts) == len(set(starts))


def test_late_data_on_retired_device_updates_merge(conn):
    cut = _setup_asset(conn, d1_hole=(5 * H, 6 * H))
    before = merge.merged_series(conn, "A1", "vibration", 0, 24 * H)
    assert not any(5 * H <= p["ts"] < 6 * H for p in before)  # hole visible in merge

    # the retired device's missing hour arrives late -> merge fills it, still D1-owned
    pipeline.ingest_points(conn, series("D1", 5 * H, 6 * H), "t", "backfill", 48 * H)
    after = merge.merged_series(conn, "A1", "vibration", 0, 24 * H)
    filled = [p for p in after if 5 * H <= p["ts"] < 6 * H]
    assert len(filled) == 60 and all(p["device_id"] == "D1" for p in filled)
    # cutover side of the merge is untouched
    assert all(p["device_id"] == "D2" for p in after if p["ts"] >= cut)
