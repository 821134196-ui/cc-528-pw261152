"""Ingestion + incremental recompute orchestration.

Dependency model: a raw point at time t influences
  - the health window containing t (direct), and
  - every window whose trailing baseline [W.start - baseline_s, W.start) covers t.
So a batch touching [min_ts, max_ts] only recomputes windows with
  W.start in (min_ts - window_s, max_ts + baseline_s].
Every recomputed window is written to recompute_log — that log is the proof
that late/backfilled data only re-runs the affected time ranges.
"""
from __future__ import annotations

import sqlite3

from . import alerts, db, health, quality, rul, rules as rules_mod


def _floor(v: int, grid: int) -> int:
    return (v // grid) * grid


def affected_window_starts(min_ts: int, max_ts: int, rules: dict) -> list[int]:
    window_s = int(rules["window_s"])
    baseline_s = int(rules["baseline_s"])
    # smallest grid-aligned W.start strictly greater than (min_ts - window_s)
    first = _floor(min_ts - window_s, window_s) + window_s
    last = _floor(max_ts + baseline_s, window_s)
    return list(range(first, last + 1, window_s))


def ingest_points(conn: sqlite3.Connection, points: list[dict], source: str,
                  kind: str, arrived_at: int) -> dict:
    """Ingest one batch of raw points, clean affected ranges, recompute only
    the windows those points can influence. Returns a summary."""
    assert kind in ("live", "backfill")
    rule_version, rules = rules_mod.get_active_rules(conn)
    data_version = db.bump_data_version(conn)

    known = {r["device_id"] for r in db.rows(conn, "SELECT device_id FROM devices")}
    ok = [p for p in points if p["device_id"] in known]
    rejected = len(points) - len(ok)

    ts_all = [int(p["ts"]) for p in ok]
    cur = conn.execute(
        "INSERT INTO ingest_batches(arrived_at, source, kind, data_version, n_points, min_ts, max_ts)"
        " VALUES (?,?,?,?,?,?,?)",
        (arrived_at, source, kind, data_version, len(ok),
         min(ts_all) if ts_all else None, max(ts_all) if ts_all else None),
    )
    batch_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO sensor_readings_raw(batch_id, device_id, metric, ts, value, reported_at)"
        " VALUES (?,?,?,?,?,?)",
        [(batch_id, p["device_id"], p["metric"], int(p["ts"]), float(p["value"]),
          int(p.get("reported_at") or arrived_at))
         for p in ok],
    )

    # group new points per (device, metric) and reclean those ranges
    by_dm: dict[tuple[str, str], list[int]] = {}
    for p in ok:
        by_dm.setdefault((p["device_id"], p["metric"]), []).append(int(p["ts"]))
    intervals = {r["device_id"]: int(r["expected_interval_s"])
                 for r in db.rows(conn, "SELECT device_id, expected_interval_s FROM devices")}
    clean_stats = {}
    for (dev, metric), ts_list in by_dm.items():
        pad = (int(rules["gap_fill_max"]) + 1) * intervals[dev]
        stats = quality.reclean_range(conn, dev, metric, min(ts_list) - pad,
                                      max(ts_list) + pad, rules, data_version)
        clean_stats[f"{dev}/{metric}"] = stats

    # recompute only affected windows per device
    by_dev: dict[str, list[int]] = {}
    for (dev, _), ts_list in by_dm.items():
        by_dev.setdefault(dev, []).extend(ts_list)
    reason = "late_data" if kind == "backfill" else "new_data"
    affected: dict[str, list[int]] = {}
    window_s = int(rules["window_s"])
    for dev, ts_list in by_dev.items():
        starts = affected_window_starts(min(ts_list), max(ts_list), rules)
        # never score windows beyond the device's newest cleaned point:
        # they have no data yet and would only emit spurious low-coverage alerts
        max_clean = db.row(conn, "SELECT MAX(ts) AS m FROM cleaned_points WHERE device_id=?",
                           (dev,))["m"]
        if max_clean is None:
            continue
        cap = _floor(int(max_clean), window_s)
        starts = [s for s in starts if s <= cap]
        if not starts:
            continue
        recompute_windows(conn, dev, starts, reason, batch_id, data_version, rule_version, rules)
        affected[dev] = starts

    conn.commit()
    return {"batch_id": batch_id, "data_version": data_version, "rule_version": rule_version,
            "n_points": len(ok), "rejected": rejected, "clean_stats": clean_stats,
            "affected_windows": {d: len(s) for d, s in affected.items()}}


def recompute_windows(conn: sqlite3.Connection, device_id: str, window_starts: list[int],
                      reason: str, batch_id: int | None, data_version: int,
                      rule_version: int, rules: dict) -> None:
    window_s = int(rules["window_s"])
    for ws in sorted(window_starts):
        snap = health.compute_window(conn, device_id, ws, rules)
        health.insert_snapshot(conn, device_id, snap, data_version, rule_version)
        new_alerts = alerts.evaluate_window(conn, device_id, snap, rules)
        alerts.replace_window_alerts(conn, device_id, ws, new_alerts, data_version, rule_version)
        conn.execute(
            "INSERT INTO recompute_log(batch_id, device_id, window_start, window_end, reason,"
            " data_version, rule_version) VALUES (?,?,?,?,?,?,?)",
            (batch_id, device_id, ws, ws + window_s, reason, data_version, rule_version),
        )
    _recompute_rul(conn, device_id, window_starts, rules, data_version, rule_version)


def _recompute_rul(conn, device_id, window_starts, rules, data_version, rule_version) -> None:
    window_s = int(rules["window_s"])
    k = int(rules["rul"]["trail_windows"])
    as_ofs = {ws + window_s for ws in window_starts}
    latest = db.row(
        conn,
        "SELECT MAX(window_end) AS m FROM health_snapshots WHERE device_id=? AND superseded_by IS NULL",
        (device_id,),
    )
    if latest and latest["m"] is not None:
        latest_end = int(latest["m"])
        # latest RUL depends on its trailing K windows; refresh if any was touched
        if any(ws >= latest_end - k * window_s for ws in window_starts):
            as_ofs.add(latest_end)
    for as_of in sorted(as_ofs):
        est = rul.estimate_rul(conn, device_id, as_of, rules)
        rul.insert_rul(conn, device_id, as_of, est, data_version, rule_version)


def apply_rule_change(conn: sqlite3.Connection, new_config: dict) -> dict:
    """New rule version: recompute every existing window under the new rules
    (data_version unchanged — only the rule dimension moves)."""
    rule_version = rules_mod.create_rule_version(conn, new_config, activate=True)
    data_version = db.current_data_version(conn)
    devices = db.rows(conn, "SELECT DISTINCT device_id FROM health_snapshots")
    total = 0
    for d in devices:
        dev = d["device_id"]
        starts = [r["window_start"] for r in db.rows(
            conn, "SELECT DISTINCT window_start FROM health_snapshots"
            " WHERE device_id=? AND superseded_by IS NULL", (dev,))]
        recompute_windows(conn, dev, starts, "rule_change", None,
                          data_version, rule_version, new_config)
        total += len(starts)
    conn.commit()
    return {"rule_version": rule_version, "data_version": data_version, "windows_recomputed": total}
