"""Version binding: every artifact carries (data_version, rule_version);
disposition survives recompute; rule changes recompute under same data version."""
from app import alerts, db, pipeline

from conftest import activate, add_device, make_rules, series

H = 3600


def _anomaly_stream():
    # alternating 1.9/2.1 -> baseline mean 2.0, std 0.1; window 10h sits at 2.25 -> z = 2.5
    def fn(t):
        if 10 * H <= t < 11 * H:
            return 2.25
        return 1.9 if (t // 60) % 2 == 0 else 2.1
    return series("D1", 0, 24 * H, fn=fn)


def test_artifacts_carry_versions(conn):
    add_device(conn)
    rv1 = activate(conn, make_rules())
    res = pipeline.ingest_points(conn, _anomaly_stream(), "t", "live", 24 * H + 5)
    dv1 = res["data_version"]

    s = db.row(conn, "SELECT data_version, rule_version FROM health_snapshots"
                     " WHERE device_id='D1' AND superseded_by IS NULL LIMIT 1")
    assert (s["data_version"], s["rule_version"]) == (dv1, rv1)
    a = db.row(conn, "SELECT data_version, rule_version FROM alerts"
                     " WHERE device_id='D1' AND superseded_by IS NULL LIMIT 1")
    assert (a["data_version"], a["rule_version"]) == (dv1, rv1)
    r = db.row(conn, "SELECT data_version, rule_version FROM rul_estimates"
                     " WHERE device_id='D1' AND superseded_by IS NULL LIMIT 1")
    assert (r["data_version"], r["rule_version"]) == (dv1, rv1)

    # rule change: rule_version moves, data_version stays
    cfg2 = make_rules(metrics={"vibration": {"warn_z": 2.0, "crit_z": 2.4, "weight": 1.0, "abs_max": 8.0}})
    out = pipeline.apply_rule_change(conn, cfg2)
    assert out["rule_version"] == rv1 + 1
    s2 = db.row(conn, "SELECT DISTINCT data_version, rule_version FROM health_snapshots"
                      " WHERE device_id='D1' AND superseded_by IS NULL")
    assert (s2["data_version"], s2["rule_version"]) == (dv1, rv1 + 1)


def test_rule_change_effect_on_scores(conn):
    add_device(conn)
    activate(conn, make_rules())
    pipeline.ingest_points(conn, _anomaly_stream(), "t", "live", 24 * H + 5)
    before = db.row(conn, "SELECT health_score FROM health_snapshots"
                          " WHERE device_id='D1' AND window_start=? AND superseded_by IS NULL", (10 * H,))
    assert before["health_score"] == 70.0          # penalty 30 at crit_z=3.0

    cfg2 = make_rules(metrics={"vibration": {"warn_z": 2.0, "crit_z": 2.4, "weight": 1.0, "abs_max": 8.0}})
    pipeline.apply_rule_change(conn, cfg2)
    after = db.row(conn, "SELECT health_score FROM health_snapshots"
                         " WHERE device_id='D1' AND window_start=? AND superseded_by IS NULL", (10 * H,))
    assert after["health_score"] == 38.33          # penalty 61.67 at crit_z=2.4
    rules_fired = {r["rule_id"] for r in db.rows(
        conn, "SELECT rule_id FROM alerts WHERE device_id='D1' AND window_start=? AND superseded_by IS NULL",
        (10 * H,))}
    assert "metric_z_crit" in rules_fired and "metric_z_warn" not in rules_fired


def test_disposition_survives_recompute(conn):
    add_device(conn)
    activate(conn, make_rules())
    pipeline.ingest_points(conn, _anomaly_stream(), "t", "live", 24 * H + 5)
    a = db.row(conn, "SELECT id FROM alerts WHERE device_id='D1' AND rule_id='metric_z_warn'"
                     " AND superseded_by IS NULL")
    alerts.set_disposition(conn, a["id"], "ack", "monitoring", "watching bearing")

    # late data lands in the same window -> window recomputed, alert replaced
    pipeline.ingest_points(
        conn,
        [{"device_id": "D1", "metric": "vibration", "ts": 10 * H + 30, "value": 2.3,
          "reported_at": 48 * H}],
        "t", "backfill", 48 * H)

    old = db.row(conn, "SELECT superseded_by FROM alerts WHERE id=?", (a["id"],))
    assert old["superseded_by"] is not None
    new = db.row(conn, "SELECT status, disposition, disposition_note FROM alerts"
                       " WHERE device_id='D1' AND rule_id='metric_z_warn' AND window_start=?"
                       " AND superseded_by IS NULL", (10 * H,))
    assert new["status"] == "ack"
    assert new["disposition"] == "monitoring"
    assert new["disposition_note"] == "watching bearing"
