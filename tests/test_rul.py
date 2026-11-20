"""RUL: linear trend over trailing health scores."""
from app import health, rul

from conftest import activate, add_device, make_rules

H = 3600


def _snap(ws, score):
    return {"window_start": ws, "window_end": ws + H, "health_score": score,
            "coverage": 1.0, "metrics": {}}


def test_rul_from_declining_trend(conn):
    add_device(conn)
    rv = activate(conn, make_rules())
    for i in range(12):
        health.insert_snapshot(conn, "D1", _snap(i * H, 90.0 - 2.0 * i), 1, rv)
    conn.commit()
    est = rul.estimate_rul(conn, "D1", 12 * H, make_rules())
    # slope -2/h, score_now 68, fail at 40 -> 14h
    assert est["rul_hours"] == 14.0
    assert est["detail"]["slope_per_h"] == -2.0


def test_rul_stable_is_none(conn):
    add_device(conn)
    rv = activate(conn, make_rules())
    for i in range(12):
        health.insert_snapshot(conn, "D1", _snap(i * H, 95.0), 1, rv)
    conn.commit()
    est = rul.estimate_rul(conn, "D1", 12 * H, make_rules())
    assert est["rul_hours"] is None
    assert est["detail"]["reason"] == "no_degradation_trend"


def test_rul_insufficient_history(conn):
    add_device(conn)
    rv = activate(conn, make_rules())
    for i in range(3):
        health.insert_snapshot(conn, "D1", _snap(i * H, 90.0 - 5.0 * i), 1, rv)
    conn.commit()
    est = rul.estimate_rul(conn, "D1", 3 * H, make_rules())
    assert est["rul_hours"] is None
    assert est["detail"]["reason"] == "insufficient_history"
