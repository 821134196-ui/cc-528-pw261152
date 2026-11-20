"""Data-quality handling: duplicates, gaps, clock drift, model change (换型)."""
from app import db, pipeline

from conftest import activate, add_device, make_rules, series


def test_dedup_and_gap_fill(conn):
    add_device(conn)
    activate(conn, make_rules())
    pts = []
    for t in range(0, 6 * 3600, 60):
        if 600 <= t < 720:            # 2-slot gap -> interpolated
            continue
        if 3600 <= t < 3600 + 600:    # 10-slot gap -> left open
            continue
        pts.append({"device_id": "D1", "metric": "vibration", "ts": t, "value": 2.0,
                    "reported_at": t + 5})
        pts.append({"device_id": "D1", "metric": "vibration", "ts": t + 5, "value": 2.2,
                    "reported_at": t + 10})  # duplicate resend, slightly off-grid
    res = pipeline.ingest_points(conn, pts, "test", "live", arrived_at=6 * 3600 + 10)

    q = {r["quality"]: r["n"] for r in db.rows(
        conn, "SELECT quality, COUNT(*) n FROM cleaned_points GROUP BY quality")}
    assert q["dup_resolved"] == 348          # 360 slots - 12 missing, all duplicated
    assert q["gap_filled"] == 2              # only the short gap
    stats = res["clean_stats"]["D1/vibration"]
    assert stats["n_open_gap_slots"] == 10   # long gap surfaces as open, not invented
    assert stats["n_dup"] == 348
    # duplicate representative value = median of the pair
    p0 = db.row(conn, "SELECT value FROM cleaned_points WHERE device_id='D1' AND ts=0")
    assert abs(p0["value"] - 2.1) < 1e-9


def test_clock_drift_corrected(conn):
    add_device(conn)
    activate(conn, make_rules())
    pts = []
    jump_at = 3 * 3600
    for t in range(0, 6 * 3600, 60):
        offset = 5 if t < jump_at else 125   # sensor clock jumps +120s at 3h
        pts.append({"device_id": "D1", "metric": "vibration", "ts": t, "value": 2.0,
                    "reported_at": t + offset})
    pipeline.ingest_points(conn, pts, "test", "live", arrived_at=6 * 3600 + 200)

    drifted = db.rows(conn, "SELECT ts, drift_s FROM cleaned_points WHERE quality='drift_corrected'")
    assert drifted, "expected drift-corrected points"
    # baseline latency = 5s -> correction = 120s; corrected ts = raw ts + 120
    by_ts = {r["ts"]: r["drift_s"] for r in drifted}
    assert all(d == 120 for d in by_ts.values())
    assert min(by_ts) == jump_at + 120       # first drifted raw point was ts=jump_at
    ok = db.rows(conn, "SELECT ts FROM cleaned_points WHERE quality='ok'")
    assert max(r["ts"] for r in ok) < jump_at


def test_backfill_not_drift_corrected(conn):
    add_device(conn)
    activate(conn, make_rules())
    live = series("D1", 0, 6 * 3600)
    pipeline.ingest_points(conn, live, "test", "live", arrived_at=6 * 3600 + 5)
    # late points arrive hours later; arrival lag must NOT be read as clock drift
    late = [{"device_id": "D1", "metric": "vibration", "ts": t, "value": 2.0,
             "reported_at": 12 * 3600} for t in range(7 * 3600, 8 * 3600, 60)]
    pipeline.ingest_points(conn, late, "test", "backfill", arrived_at=12 * 3600)
    drifted = db.rows(conn, "SELECT COUNT(*) n FROM cleaned_points WHERE quality='drift_corrected'")
    assert drifted[0]["n"] == 0


def test_model_change_resets_baseline(conn):
    add_device(conn)
    activate(conn, make_rules())
    seg = 10 * 3600
    conn.execute("INSERT INTO device_model_events(device_id, model, effective_from) VALUES ('D1','M1',0)")
    conn.execute("INSERT INTO device_model_events(device_id, model, effective_from) VALUES ('D1','M2',?)",
                 (seg,))
    conn.commit()
    # model M2 runs at a higher normal level; without segmentation z would explode
    pts = series("D1", 0, 20 * 3600, fn=lambda t: 2.0 if t < seg else 5.0)
    pipeline.ingest_points(conn, pts, "test", "live", arrived_at=20 * 3600 + 5)

    import json
    snap = db.row(conn, "SELECT metrics_json FROM health_snapshots"
                        " WHERE device_id='D1' AND window_start=? AND superseded_by IS NULL",
                  (15 * 3600,))
    m = json.loads(snap["metrics_json"])["vibration"]
    assert m["baseline"]["from_ts"] >= seg          # baseline stays inside the new segment
    assert abs(m["baseline"]["mean"] - 5.0) < 1e-6
    alerts = db.rows(conn, "SELECT rule_id FROM alerts WHERE device_id='D1'"
                           " AND window_start>=? AND superseded_by IS NULL"
                           " AND rule_id LIKE 'metric_z%'", (12 * 3600,))
    assert alerts == []                              # new normal is not an anomaly
