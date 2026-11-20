import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, rules as rules_mod  # noqa: E402


def make_rules(**over):
    """Small deterministic rule set: one metric, short windows/baselines."""
    cfg = copy.deepcopy(rules_mod.DEFAULT_RULES)
    cfg.update({
        "window_s": 3600,
        "baseline_s": 2 * 3600,
        "min_baseline_points": 6,
        "metrics": {"vibration": {"warn_z": 2.0, "crit_z": 3.0, "weight": 1.0, "abs_max": 8.0}},
        "gap_fill_max": 3,
        "dedup_tolerance_s": 20,
        "drift_min_s": 30,
        "coverage_floor": 0.5,
        "alert_score_warn": 80.0,
        "alert_score_crit": 60.0,
        "sustained_windows": 3,
        "rul": {"trail_windows": 12, "fail_score": 40.0, "min_slope_per_h": 0.05},
    })
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_db(c)
    yield c
    c.close()


def add_device(conn, device_id="D1", interval=60, installed_at=0):
    conn.execute(
        "INSERT INTO devices(device_id, name, expected_interval_s, installed_at) VALUES (?,?,?,?)",
        (device_id, device_id, interval, installed_at))
    conn.commit()


def activate(conn, cfg):
    return rules_mod.create_rule_version(conn, cfg, activate=True)


def series(device_id, start, end, interval=60, metric="vibration", fn=None, latency=5):
    """Constant-latency live points; fn(t) -> value."""
    fn = fn or (lambda t: 2.0)
    return [{"device_id": device_id, "metric": metric, "ts": t, "value": fn(t),
             "reported_at": t + latency}
            for t in range(start, end, interval)]
