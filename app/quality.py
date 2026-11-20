"""Data-quality layer: clock-drift correction, duplicate resampling, gap filling.

Raw readings are immutable; this layer rebuilds `cleaned_points` for a given
time range from raw rows. Only the affected range is ever rebuilt.
"""
from __future__ import annotations

import sqlite3
from statistics import median

from . import db


def _baseline_latency(conn: sqlite3.Connection, device_id: str, metric: str) -> float:
    """Min observed (reported_at - ts) over *live* batches ≈ true transport latency.

    Backfill rows are excluded: they arrive long after the fact and would look
    like enormous clock offsets.
    """
    r = db.row(
        conn,
        """
        SELECT MIN(r.reported_at - r.ts) AS lat
        FROM sensor_readings_raw r
        JOIN ingest_batches b ON b.batch_id = r.batch_id
        WHERE r.device_id=? AND r.metric=? AND b.kind='live'
        """,
        (device_id, metric),
    )
    return float(r["lat"]) if r and r["lat"] is not None else 0.0


def _load_raw(conn, device_id, metric, start_ts, end_ts) -> list[dict]:
    return db.rows(
        conn,
        """
        SELECT r.id, r.ts, r.value, r.reported_at, r.batch_id, b.kind
        FROM sensor_readings_raw r
        JOIN ingest_batches b ON b.batch_id = r.batch_id
        WHERE r.device_id=? AND r.metric=? AND r.ts BETWEEN ? AND ?
        ORDER BY r.ts, r.batch_id, r.id
        """,
        (device_id, metric, start_ts, end_ts),
    )


def _correct_drift(points: list[dict], baseline_lat: float, drift_min_s: int) -> None:
    """In-place: add corrected ts `cts`, `drift_s`, `drifted` flag.

    A live point whose (arrival - sensor_ts) exceeds baseline latency by more
    than drift_min_s has a drifting sensor clock; snap it back to arrival-based
    time. Backfill points keep their sensor ts.
    """
    for p in points:
        p["cts"] = p["ts"]
        p["drift_s"] = 0
        p["drifted"] = False
        if p["kind"] == "live":
            offset = (p["reported_at"] - p["ts"]) - baseline_lat
            if offset >= drift_min_s:
                p["cts"] = p["ts"] + int(round(offset))
                p["drift_s"] = int(round(offset))
                p["drifted"] = True


def _dedup(points: list[dict], tol_s: int) -> list[dict]:
    """Group corrected points closer than tol_s; representative = earliest ts,
    median value. Returns cleaned point list sorted by ts."""
    out: list[dict] = []
    group: list[dict] = []
    for p in sorted(points, key=lambda x: (x["cts"], x["batch_id"], x["id"])):
        if group and p["cts"] - group[-1]["cts"] > tol_s:
            out.append(_emit(group))
            group = []
        group.append(p)
    if group:
        out.append(_emit(group))
    return out


def _emit(group: list[dict]) -> dict:
    drifted = any(g["drifted"] for g in group)
    if len(group) > 1:
        quality = "dup_resolved"
    elif drifted:
        quality = "drift_corrected"
    else:
        quality = "ok"
    return {
        "ts": group[0]["cts"],
        "value": float(median(g["value"] for g in group)),
        "quality": quality,
        "drift_s": max((g["drift_s"] for g in group), default=0),
    }


def _fill_gaps(points: list[dict], interval_s: int, gap_fill_max: int) -> tuple[list[dict], int, int]:
    """Interpolate short interior gaps on the expected sampling grid.

    Returns (points_with_fills, n_filled, n_open_gap_slots). Long gaps are left
    open — they surface as coverage loss, not invented data.
    """
    if not points:
        return [], 0, 0
    filled: list[dict] = []
    n_filled = 0
    n_open = 0
    out: list[dict] = []
    for prev, nxt in zip(points, points[1:]):
        out.append(prev)
        delta = nxt["ts"] - prev["ts"]
        n_missing = round(delta / interval_s) - 1
        if n_missing < 1:
            continue
        if abs(delta - (n_missing + 1) * interval_s) > interval_s * 0.5:
            continue  # irregular spacing, not a clean grid gap
        if n_missing <= gap_fill_max:
            for k in range(1, n_missing + 1):
                frac = k / (n_missing + 1)
                filled.append(
                    {
                        "ts": prev["ts"] + k * interval_s,
                        "value": prev["value"] + (nxt["value"] - prev["value"]) * frac,
                        "quality": "gap_filled",
                        "drift_s": 0,
                    }
                )
                n_filled += 1
        else:
            n_open += n_missing
    out.append(points[-1])
    return sorted(out + filled, key=lambda x: x["ts"]), n_filled, n_open


def reclean_range(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    start_ts: int,
    end_ts: int,
    rules: dict,
    data_version: int,
) -> dict:
    """Rebuild cleaned_points for [start_ts, end_ts]. Returns quality stats."""
    interval_s = db.row(conn, "SELECT expected_interval_s AS i FROM devices WHERE device_id=?", (device_id,))["i"]
    tol = int(rules["dedup_tolerance_s"])
    margin = int(rules["gap_fill_max"]) * interval_s + tol
    raw = _load_raw(conn, device_id, metric, start_ts - margin, end_ts + margin)
    if not raw:
        conn.execute(
            "DELETE FROM cleaned_points WHERE device_id=? AND metric=? AND ts BETWEEN ? AND ?",
            (device_id, metric, start_ts, end_ts),
        )
        return {"n_raw": 0, "n_dup": 0, "n_drift": 0, "n_filled": 0, "n_open_gap_slots": 0}

    baseline_lat = _baseline_latency(conn, device_id, metric)
    _correct_drift(raw, baseline_lat, int(rules["drift_min_s"]))
    n_drift = sum(1 for p in raw if p["drifted"])
    cleaned = _dedup(raw, tol)
    n_dup = len(raw) - len(cleaned)
    cleaned, n_filled, n_open = _fill_gaps(cleaned, interval_s, int(rules["gap_fill_max"]))

    # drift-corrected points can land beyond end_ts (their raw ts was in range,
    # the clock offset shifts them forward) — extend the emit/delete range so
    # they are not silently dropped
    effective_end = max(end_ts, max((p["ts"] for p in cleaned), default=end_ts))
    # emit only points inside the requested range
    emit = [p for p in cleaned if start_ts <= p["ts"] <= effective_end]
    conn.execute(
        "DELETE FROM cleaned_points WHERE device_id=? AND metric=? AND ts BETWEEN ? AND ?",
        (device_id, metric, start_ts, effective_end),
    )
    conn.executemany(
        "INSERT INTO cleaned_points(device_id, metric, ts, value, quality, drift_s, data_version)"
        " VALUES (?,?,?,?,?,?,?)",
        [(device_id, metric, p["ts"], p["value"], p["quality"], p["drift_s"], data_version) for p in emit],
    )
    return {
        "n_raw": len(raw),
        "n_dup": n_dup,
        "n_drift": n_drift,
        "n_filled": n_filled,
        "n_open_gap_slots": n_open,
    }
