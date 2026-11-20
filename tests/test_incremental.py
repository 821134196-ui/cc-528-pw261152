"""Incremental recompute: only affected windows re-run; late data stays local."""
from app import db, pipeline

from conftest import activate, add_device, make_rules, series

H = 3600


def _log(conn, batch_id):
    return db.rows(conn, "SELECT window_start, reason FROM recompute_log WHERE batch_id=?",
                   (batch_id,))


def test_affected_window_math(conn):
    add_device(conn)
    activate(conn, make_rules())  # window 1h, baseline 2h
    res1 = pipeline.ingest_points(conn, series("D1", 0, 24 * H), "t", "live", 24 * H + 5)
    # points [0, 24h): data windows 0..23h (windows beyond the newest point are
    # not scored); baseline-reach windows 24h/25h are empty and skipped
    logged = {r["window_start"] for r in _log(conn, res1["batch_id"])}
    assert min(logged) == 0
    assert max(logged) == 23 * H
    assert len(logged) == 24

    # late backfill of one hour [3h, 4h): direct window 3h + baseline reach 4h,5h
    res2 = pipeline.ingest_points(conn, series("D1", 3 * H, 4 * H), "t", "backfill", 48 * H)
    logged2 = {r["window_start"] for r in _log(conn, res2["batch_id"])}
    assert logged2 == {3 * H, 4 * H, 5 * H}
    assert all(r["reason"] == "late_data" for r in _log(conn, res2["batch_id"]))


def test_unaffected_windows_keep_version(conn):
    add_device(conn)
    activate(conn, make_rules())
    res1 = pipeline.ingest_points(conn, series("D1", 0, 24 * H), "t", "live", 24 * H + 5)
    dv1 = res1["data_version"]

    # backfill far in the past: [2h, 3h) -> affected windows {2h,3h,4h}
    pipeline.ingest_points(conn, series("D1", 2 * H, 3 * H), "t", "backfill", 48 * H)

    # windows beyond the 2h baseline reach must NOT have been recomputed
    untouched = db.rows(conn, "SELECT DISTINCT data_version FROM health_snapshots"
                              " WHERE device_id='D1' AND window_start>=? AND superseded_by IS NULL",
                        (5 * H,))
    assert {r["data_version"] for r in untouched} == {dv1}
    # affected windows carry the new data version
    affected = db.rows(conn, "SELECT DISTINCT data_version FROM health_snapshots"
                             " WHERE device_id='D1' AND window_start IN (?,?,?) AND superseded_by IS NULL",
                       (2 * H, 3 * H, 4 * H))
    assert {r["data_version"] for r in affected} == {dv1 + 1}


def test_late_data_updates_content(conn):
    add_device(conn)
    activate(conn, make_rules())
    # live stream with a one-hour hole at [5h, 6h)
    pts = [p for p in series("D1", 0, 24 * H) if not (5 * H <= p["ts"] < 6 * H)]
    pipeline.ingest_points(conn, pts, "t", "live", 24 * H + 5)
    before = db.row(conn, "SELECT health_score, coverage FROM health_snapshots"
                          " WHERE device_id='D1' AND window_start=? AND superseded_by IS NULL",
                    (5 * H,))
    assert before["health_score"] is None and before["coverage"] == 0.0

    # the missing hour arrives late
    pipeline.ingest_points(conn, series("D1", 5 * H, 6 * H), "t", "backfill", 48 * H)
    after = db.row(conn, "SELECT health_score, coverage FROM health_snapshots"
                         " WHERE device_id='D1' AND window_start=? AND superseded_by IS NULL",
                   (5 * H,))
    assert after["health_score"] is not None
    assert after["coverage"] > 0.9
