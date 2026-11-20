"""Windowed health scoring. Segment-aware baselines (换型), downtime-aware coverage."""
from __future__ import annotations

import json
import math
import sqlite3

from . import db


def segment_start(conn: sqlite3.Connection, device_id: str, ts: int) -> int:
    """Start of the model segment containing ts (换型 resets baselines)."""
    r = db.row(
        conn,
        "SELECT MAX(effective_from) AS s FROM device_model_events WHERE device_id=? AND effective_from<=?",
        (device_id, ts),
    )
    if r and r["s"] is not None:
        return int(r["s"])
    dev = db.row(conn, "SELECT installed_at AS i FROM devices WHERE device_id=?", (device_id,))
    return int(dev["i"] or 0) if dev else 0


def _downtime_slots(conn: sqlite3.Connection, device_id: str, w_start: int, w_end: int, interval_s: int) -> int:
    """Expected-sample slots to exclude because the device was down."""
    dts = db.rows(
        conn,
        "SELECT start_ts, COALESCE(end_ts, ?) AS end_ts FROM downtime_records"
        " WHERE device_id=? AND start_ts < ? AND COALESCE(end_ts, ?) > ?",
        (w_end, device_id, w_end, w_end, w_start),
    )
    slots = 0
    for d in dts:
        overlap = min(w_end, int(d["end_ts"])) - max(w_start, int(d["start_ts"]))
        if overlap > 0:
            slots += math.ceil(overlap / interval_s)
    return slots


def _baseline(conn, device_id, metric, w_start, baseline_s) -> dict:
    seg = segment_start(conn, device_id, w_start)
    lo = max(seg, w_start - baseline_s)
    vals = [
        r["value"]
        for r in db.rows(
            conn,
            "SELECT value FROM cleaned_points WHERE device_id=? AND metric=?"
            " AND ts>=? AND ts<? AND quality!='gap_filled'",
            (device_id, metric, lo, w_start),
        )
    ]
    if not vals:
        return {"n": 0, "mean": None, "std": None, "from_ts": lo}
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return {"n": len(vals), "mean": mean, "std": max(math.sqrt(var), 1e-6), "from_ts": lo}


def _penalty(z_agg: float | None, warn_z: float, crit_z: float, abs_breach: bool) -> float:
    if abs_breach:
        return 100.0
    if z_agg is None or z_agg <= warn_z:
        return 0.0
    if z_agg <= crit_z:
        return 60.0 * (z_agg - warn_z) / (crit_z - warn_z)
    return min(100.0, 60.0 + 40.0 * (z_agg - crit_z) / crit_z)


def compute_window(conn: sqlite3.Connection, device_id: str, w_start: int, rules: dict) -> dict:
    """Compute the health snapshot for one window (pure read, no writes)."""
    window_s = int(rules["window_s"])
    w_end = w_start + window_s
    dev = db.row(conn, "SELECT expected_interval_s AS i FROM devices WHERE device_id=?", (device_id,))
    interval_s = int(dev["i"])
    expected = window_s // interval_s - _downtime_slots(conn, device_id, w_start, w_end, interval_s)
    if expected <= 0:
        # window fully covered by downtime: not scorable, not a data-quality issue
        return {"window_start": w_start, "window_end": w_end, "health_score": None,
                "coverage": 1.0, "metrics": {m: {"n": 0, "n_measured": 0, "n_filled": 0}
                                             for m in rules["metrics"]},
                "downtime": True}
    expected = max(1, expected)

    metrics_out: dict = {}
    weighted = 0.0
    weight_sum = 0.0
    measured_total = 0

    for metric, cfg in rules["metrics"].items():
        pts = db.rows(
            conn,
            "SELECT ts, value, quality FROM cleaned_points"
            " WHERE device_id=? AND metric=? AND ts>=? AND ts<? ORDER BY ts",
            (device_id, metric, w_start, w_end),
        )
        measured = [p for p in pts if p["quality"] != "gap_filled"]
        measured_total += len(measured)
        base = _baseline(conn, device_id, metric, w_start, int(rules["baseline_s"]))
        warn_z, crit_z = float(cfg["warn_z"]), float(cfg["crit_z"])
        abs_max = float(cfg["abs_max"])

        top: list[dict] = []
        z_agg = None
        if base["n"] and base["n"] >= int(rules["min_baseline_points"]):
            zs = []
            for p in measured:
                z = (p["value"] - base["mean"]) / base["std"]
                zs.append((abs(z), p["ts"], p["value"], z))
            zs.sort(reverse=True)
            if zs:
                # collective deviation (RMS) tempered by a damped single-point
                # peak: one noisy outlier alone cannot raise an alert, a real
                # spike or a sustained shift can
                rms = math.sqrt(sum(z[0] ** 2 for z in zs) / len(zs))
                z_agg = max(rms, 0.6 * zs[0][0])
                top = [
                    {"ts": t, "value": v, "z": round(z, 3)}
                    for _, t, v, z in zs[:5]
                ]
        v_max = max((p["value"] for p in measured), default=None)
        abs_breach = v_max is not None and v_max > abs_max
        pen = _penalty(z_agg, warn_z, crit_z, abs_breach)
        score = 100.0 - pen
        w = float(cfg["weight"])
        weighted += w * score
        weight_sum += w
        metrics_out[metric] = {
            "n": len(pts),
            "n_measured": len(measured),
            "n_filled": len(pts) - len(measured),
            "max": v_max,
            "z_agg": round(z_agg, 3) if z_agg is not None else None,
            "baseline": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in base.items()},
            "abs_max": abs_max,
            "warn_z": warn_z,
            "crit_z": crit_z,
            "penalty": round(pen, 2),
            "score": round(score, 2),
            "top_points": top,
        }

    coverage = min(1.0, measured_total / (expected * max(1, len(rules["metrics"]))))
    if coverage < float(rules["coverage_floor"]) or weight_sum == 0:
        health = None
    else:
        health = round(weighted / weight_sum, 2)
    return {
        "window_start": w_start,
        "window_end": w_end,
        "health_score": health,
        "coverage": round(coverage, 4),
        "metrics": metrics_out,
    }


def insert_snapshot(conn, device_id: str, snap: dict, data_version: int, rule_version: int) -> int:
    """Insert new snapshot and supersede the previous current one for the window."""
    cur = conn.execute(
        "INSERT INTO health_snapshots(device_id, window_start, window_end, health_score, coverage,"
        " metrics_json, data_version, rule_version, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            device_id,
            snap["window_start"],
            snap["window_end"],
            snap["health_score"],
            snap["coverage"],
            json.dumps(snap["metrics"]),
            data_version,
            rule_version,
            db.now_ts(),
        ),
    )
    new_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE health_snapshots SET superseded_by=? WHERE device_id=? AND window_start=?"
        " AND superseded_by IS NULL AND id!=?",
        (new_id, device_id, snap["window_start"], new_id),
    )
    return new_id
