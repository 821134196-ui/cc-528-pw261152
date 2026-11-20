"""Remaining-useful-life from the trailing health-score trend (least squares)."""
from __future__ import annotations

import json
import sqlite3

from . import db


def estimate_rul(conn, device_id: str, as_of: int, rules: dict) -> dict:
    """RUL estimate at window-end `as_of` from trailing health snapshots.

    Scores are binned into ~K/12 buckets (6h with the default 72h trail) before
    fitting: window-level health is often bimodal (fault episodes), and the
    bin means give the least-squares fit a meaningful trend line.
    """
    cfg = rules["rul"]
    window_s = int(rules["window_s"])
    k = int(cfg["trail_windows"])
    rows = db.rows(
        conn,
        "SELECT window_end, health_score FROM health_snapshots"
        " WHERE device_id=? AND window_end<=? AND window_end>?"
        " AND superseded_by IS NULL AND health_score IS NOT NULL ORDER BY window_end",
        (device_id, as_of, as_of - k * window_s),
    )
    bin_w = max(1, k // 12)
    detail = {"n": len(rows), "trail_windows": k, "fail_score": cfg["fail_score"],
              "bin_windows": bin_w}
    # consecutive bins of bin_w windows -> (bin end time, mean score)
    bins: list[tuple[float, float]] = []
    for i in range(0, len(rows) - bin_w + 1, bin_w):
        chunk = rows[i:i + bin_w]
        bins.append((chunk[-1]["window_end"] / 3600.0,
                     sum(float(r["health_score"]) for r in chunk) / bin_w))
    n = len(bins)
    if n < 6:
        return {"rul_hours": None, "method": "linear_health_trend",
                "detail": {**detail, "reason": "insufficient_history"}}
    xs = [b[0] for b in bins]
    ys = [b[1] for b in bins]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0  # score points per hour
    syy = sum((y - my) ** 2 for y in ys)
    r2 = (sxy * sxy / (sxx * syy)) if sxx and syy else 0.0
    score_now = ys[-1]
    detail.update({"slope_per_h": round(slope, 5), "r2": round(r2, 3),
                   "score_now": round(score_now, 2)})
    if slope >= -float(cfg["min_slope_per_h"]):
        return {"rul_hours": None, "method": "linear_health_trend",
                "detail": {**detail, "reason": "no_degradation_trend"}}
    rul = (score_now - float(cfg["fail_score"])) / (-slope)
    return {"rul_hours": round(max(0.0, rul), 1), "method": "linear_health_trend", "detail": detail}


def insert_rul(conn, device_id: str, as_of: int, est: dict, data_version: int, rule_version: int) -> int:
    cur = conn.execute(
        "INSERT INTO rul_estimates(device_id, as_of, rul_hours, method, detail_json,"
        " data_version, rule_version, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (device_id, as_of, est["rul_hours"], est["method"], json.dumps(est["detail"]),
         data_version, rule_version, db.now_ts()),
    )
    new_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE rul_estimates SET superseded_by=? WHERE device_id=? AND as_of=?"
        " AND superseded_by IS NULL AND id!=?",
        (new_id, device_id, as_of, new_id),
    )
    return new_id
