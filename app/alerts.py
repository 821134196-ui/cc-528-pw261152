"""Alert rules over health snapshots. Evidence is captured at fire time;
operator disposition survives recompute (carried across supersede)."""
from __future__ import annotations

import json
import sqlite3

from . import db


def evaluate_window(conn, device_id: str, snap: dict, rules: dict) -> list[dict]:
    """Pure: return alert dicts firing for this window given the snapshot."""
    out: list[dict] = []
    w_start, w_end = snap["window_start"], snap["window_end"]
    metrics = snap["metrics"]

    for metric, m in metrics.items():
        if "penalty" not in m:
            continue  # e.g. downtime-covered window
        ev = {
            "baseline": m["baseline"],
            "warn_z": m["warn_z"],
            "crit_z": m["crit_z"],
            "abs_max": m["abs_max"],
            "points": m["top_points"],
        }
        if m["penalty"] >= 60.0:
            out.append(_mk(device_id, "metric_z_crit", metric, w_start, w_end, "crit", ev))
        elif m["penalty"] > 0.0:
            out.append(_mk(device_id, "metric_z_warn", metric, w_start, w_end, "warn", ev))

    score = snap["health_score"]
    if score is not None:
        if score < float(rules["alert_score_crit"]):
            out.append(_mk(device_id, "score_crit", None, w_start, w_end, "crit",
                           {"health_score": score, "threshold": rules["alert_score_crit"]}))
        elif score < float(rules["alert_score_warn"]):
            out.append(_mk(device_id, "score_warn", None, w_start, w_end, "warn",
                           {"health_score": score, "threshold": rules["alert_score_warn"]}))
        # sustained degradation: N consecutive windows below warn threshold
        n = int(rules["sustained_windows"])
        prev = db.rows(
            conn,
            "SELECT health_score FROM health_snapshots WHERE device_id=? AND window_start<?"
            " AND superseded_by IS NULL ORDER BY window_start DESC LIMIT ?",
            (device_id, w_start, n - 1),
        )
        if len(prev) == n - 1 and all(
            p["health_score"] is not None and p["health_score"] < float(rules["alert_score_warn"])
            for p in prev
        ) and score < float(rules["alert_score_warn"]):
            out.append(_mk(device_id, "sustained_degradation", None, w_start, w_end, "warn",
                           {"health_score": score, "consecutive_windows": n,
                            "threshold": rules["alert_score_warn"]}))
    if snap["coverage"] < float(rules["coverage_floor"]):
        out.append(_mk(device_id, "low_coverage", None, w_start, w_end, "info",
                       {"coverage": snap["coverage"], "floor": rules["coverage_floor"]}))
    return out


def _mk(device_id, rule_id, metric, w_start, w_end, severity, evidence) -> dict:
    return {
        "device_id": device_id,
        "rule_id": rule_id,
        "metric": metric,
        "window_start": w_start,
        "window_end": w_end,
        "severity": severity,
        "evidence": evidence,
    }


def replace_window_alerts(conn, device_id: str, w_start: int, new_alerts: list[dict],
                          data_version: int, rule_version: int) -> None:
    """Supersede current alerts of the window, insert fresh ones, carrying
    operator disposition across by alert identity (device, rule, metric, window)."""
    old = db.rows(
        conn,
        "SELECT * FROM alerts WHERE device_id=? AND window_start=? AND superseded_by IS NULL",
        (device_id, w_start),
    )
    carry = {
        (a["rule_id"], a["metric"]): (a["status"], a["disposition"], a["disposition_note"])
        for a in old
    }
    now = db.now_ts()
    new_ids = []
    for a in new_alerts:
        status, disp, note = carry.get((a["rule_id"], a["metric"]), ("open", None, None))
        cur = conn.execute(
            "INSERT INTO alerts(device_id, rule_id, metric, window_start, window_end, severity,"
            " evidence_json, status, disposition, disposition_note, data_version, rule_version,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, a["rule_id"], a["metric"], w_start, a["window_end"], a["severity"],
             json.dumps(a["evidence"]), status, disp, note, data_version, rule_version, now, now),
        )
        new_ids.append(int(cur.lastrowid))
    for a in old:
        # point superseded_by at the replacement with same identity, if any
        repl = None
        for nid, na in zip(new_ids, new_alerts):
            if (na["rule_id"], na["metric"]) == (a["rule_id"], a["metric"]):
                repl = nid
                break
        conn.execute("UPDATE alerts SET superseded_by=? WHERE id=?",
                     (repl if repl is not None else -1, a["id"]))


def set_disposition(conn, alert_id: int, status: str, disposition: str | None, note: str | None) -> dict:
    allowed_status = {"open", "ack", "resolved"}
    allowed_disp = {"fixed", "false_positive", "monitoring", "parts_ordered"}
    if status not in allowed_status:
        raise ValueError(f"bad status {status}")
    if disposition is not None and disposition not in allowed_disp:
        raise ValueError(f"bad disposition {disposition}")
    a = db.row(conn, "SELECT id FROM alerts WHERE id=? AND superseded_by IS NULL", (alert_id,))
    if a is None:
        raise KeyError(f"alert {alert_id} not found or superseded")
    conn.execute(
        "UPDATE alerts SET status=?, disposition=COALESCE(?, disposition),"
        " disposition_note=COALESCE(?, disposition_note), updated_at=? WHERE id=?",
        (status, disposition, note, db.now_ts(), alert_id),
    )
    conn.commit()
    return db.row(conn, "SELECT * FROM alerts WHERE id=?", (alert_id,))
