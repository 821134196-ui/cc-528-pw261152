"""Rule-set versioning. Every computed artifact binds to one of these versions."""
from __future__ import annotations

import json
import sqlite3

from . import db

DEFAULT_RULES: dict = {
    "window_s": 3600,            # health evaluation window
    "baseline_s": 86400,         # trailing baseline for z-scores (within same model segment)
    "min_baseline_points": 12,   # below this, fall back to absolute thresholds only
    "metrics": {
        "vibration":   {"warn_z": 2.5, "crit_z": 3.5, "weight": 0.5, "abs_max": 8.0},
        "temperature": {"warn_z": 2.5, "crit_z": 3.5, "weight": 0.3, "abs_max": 90.0},
        "current":     {"warn_z": 3.0, "crit_z": 4.0, "weight": 0.2, "abs_max": 45.0},
    },
    # data-quality handling
    "gap_fill_max": 3,           # max consecutive missing slots to interpolate
    "dedup_tolerance_s": 20,     # points closer than this are duplicate samples
    "drift_min_s": 30,           # clock offset above this is corrected as drift
    # scoring
    "coverage_floor": 0.5,       # below this, health_score is NULL (insufficient data)
    "alert_score_warn": 80.0,
    "alert_score_crit": 60.0,
    "sustained_windows": 3,      # consecutive warn windows -> sustained alert
    # remaining useful life
    "rul": {"trail_windows": 168, "fail_score": 40.0, "min_slope_per_h": 0.05},
}


def create_rule_version(conn: sqlite3.Connection, config: dict, activate: bool = True) -> int:
    if activate:
        conn.execute("UPDATE rule_versions SET active=0")
    cur = conn.execute(
        "INSERT INTO rule_versions(config_json, created_at, active) VALUES (?,?,?)",
        (json.dumps(config, sort_keys=True), db.now_ts(), 1 if activate else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_active_rules(conn: sqlite3.Connection) -> tuple[int, dict]:
    r = db.row(conn, "SELECT rule_version, config_json FROM rule_versions WHERE active=1")
    if r is None:
        v = create_rule_version(conn, DEFAULT_RULES, activate=True)
        return v, DEFAULT_RULES
    return int(r["rule_version"]), json.loads(r["config_json"])


def ensure_default_rules(conn: sqlite3.Connection) -> int:
    v, _ = get_active_rules(conn)
    return v
