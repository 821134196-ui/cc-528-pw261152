"""Cross-device merge: one physical asset, several devices over time (换机/替换).

asset_links defines which device serves the asset in which interval; merged
views pick points/snapshots from the device valid at each moment, so overlap
periods never double-count and the timeline stays continuous across cutover.
"""
from __future__ import annotations

import sqlite3

from . import db

INF = 2**62


def _links(conn, asset_id: str) -> list[dict]:
    return db.rows(
        conn,
        "SELECT * FROM asset_links WHERE asset_id=? ORDER BY valid_from",
        (asset_id,),
    )


def merged_series(conn: sqlite3.Connection, asset_id: str, metric: str,
                  start_ts: int, end_ts: int) -> list[dict]:
    out: list[dict] = []
    for link in _links(conn, asset_id):
        lo = max(start_ts, int(link["valid_from"]))
        hi = min(end_ts, int(link["valid_to"]) if link["valid_to"] is not None else INF)
        if lo >= hi:
            continue
        for p in db.rows(
            conn,
            "SELECT ts, value, quality, drift_s, data_version FROM cleaned_points"
            " WHERE device_id=? AND metric=? AND ts>=? AND ts<? ORDER BY ts",
            (link["device_id"], metric, lo, hi),
        ):
            out.append({**p, "device_id": link["device_id"]})
    out.sort(key=lambda p: (p["ts"], p["device_id"]))
    return out


def merged_health(conn: sqlite3.Connection, asset_id: str,
                  start_ts: int, end_ts: int) -> list[dict]:
    """Asset-level health timeline: each window attributed to the serving device."""
    out: list[dict] = []
    for link in _links(conn, asset_id):
        lo = max(start_ts, int(link["valid_from"]))
        hi = min(end_ts, int(link["valid_to"]) if link["valid_to"] is not None else INF)
        if lo >= hi:
            continue
        for s in db.rows(
            conn,
            "SELECT window_start, window_end, health_score, coverage, data_version, rule_version"
            " FROM health_snapshots WHERE device_id=? AND superseded_by IS NULL"
            " AND window_start>=? AND window_start<? ORDER BY window_start",
            (link["device_id"], lo, hi),
        ):
            out.append({**s, "device_id": link["device_id"]})
    out.sort(key=lambda s: (s["window_start"], s["device_id"]))
    return out


def asset_devices(conn, asset_id: str) -> list[dict]:
    return _links(conn, asset_id)
