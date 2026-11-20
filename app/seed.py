"""Seed a realistic messy dataset and run the whole pipeline end to end.

Scenarios covered:
  P-101 / P-101R : device replacement on asset PUMP-A (cross-device merge),
                   P-101R develops a bearing fault (alerts + RUL)
  P-102          : sensor clock drift (+2s/h from day 10), duplicates,
                   short gaps (interpolated) + one long gap (backfilled late)
  C-201          : model change CX-5 -> CX-7 at day 18 (换型, baseline reset),
                   temperature excursion at day 25
Late data        : P-102's missing day-15 hours and P-101's missing day-5 hour
                   arrive as backfill batches -> localized recompute only
Rule change      : a stricter rule version is applied at the end (version diff)
"""
from __future__ import annotations

import argparse
import random
import time

from . import db, pipeline, rules as rules_mod

DAY = 86400
H = 3600
INTERVAL = 300
METRICS = ("vibration", "temperature", "current")


def _noise(rng, sigma):
    return rng.gauss(0.0, sigma)


def gen_device_points(rng, device, t0, days):
    """Full-history raw points for one device. Returns list of point dicts."""
    end = t0 + days * DAY
    pts = []

    def emit(metric, t, value, drift_s=0):
        pts.append({"device_id": device, "metric": metric, "ts": t,
                    "value": round(value, 4), "reported_at": t + 8 + drift_s})

    if device == "P-101":
        span_end = t0 + 20 * DAY + 2 * H          # overruns cutover by 2h
        for t in range(t0, span_end, INTERVAL):
            if t0 + 5 * DAY + 6 * H <= t < t0 + 5 * DAY + 7 * H:
                continue                           # hour lost, arrives late
            if rng.random() < 0.02:
                continue                           # random drop
            aging = 0.6 * (t - t0) / (20 * DAY)
            emit("vibration", t, 2.0 + aging + _noise(rng, 0.15))
            emit("temperature", t, 62 + _noise(rng, 0.8))
            emit("current", t, 12 + _noise(rng, 0.4))
            if rng.random() < 0.015:               # duplicate resend
                emit("vibration", t + rng.randint(2, 9), 2.0 + aging + _noise(rng, 0.15))

    elif device == "P-101R":
        start = t0 + 20 * DAY - 2 * H              # starts 2h before cutover
        deg_start = t0 + 30 * DAY
        # bearing fault: episodes of sustained elevated vibration, growing in
        # frequency, length and amplitude as the fault develops
        episodes = []
        for d in range(days - 30):
            prog = d + 0.5
            n_ep = 1 + d // 2                     # 1,1,2,2,3,3,... growing load
            for _ in range(n_ep):
                ep_start = deg_start + d * DAY + rng.randrange(0, 20) * H
                ep_len = rng.choice([1, 2, 2, 3] if d < 5 else [2, 3, 3, 4]) * H
                amp = min(9.0, 3.2 + 0.55 * prog)
                episodes.append((ep_start, ep_start + ep_len, amp))
        for t in range(start, end, INTERVAL):
            vib = 1.8 + _noise(rng, 0.15)
            temp = 60 + _noise(rng, 0.5)
            if t >= deg_start:
                prog = (t - deg_start) / DAY       # days into degradation
                temp += 2.0 * prog                 # slow creep 0 -> ~20 deg
                for ep0, ep1, amp in episodes:
                    if ep0 <= t < ep1:
                        vib += amp * rng.uniform(0.75, 1.05)
                        break
            emit("vibration", t, vib)
            emit("temperature", t, temp)
            emit("current", t, 14 + _noise(rng, 0.5))

    elif device == "P-102":
        drift_start = t0 + 10 * DAY
        gap_short = {rng.randrange(t0 + 2 * DAY, end - DAY, 3600) for _ in range(5)}
        long_gap = (t0 + 15 * DAY + 10 * H, t0 + 15 * DAY + 12 * H)
        for t in range(t0, end, INTERVAL):
            drift = 0 if t < drift_start else int(2 * (t - drift_start) / H)  # +2s/h
            if long_gap[0] <= t < long_gap[1]:
                continue                           # 2h outage, backfilled later
            if any(g <= t < g + 2 * INTERVAL for g in gap_short):
                continue                           # 2-slot gaps -> interpolated
            if rng.random() < 0.01:
                continue
            emit("vibration", t, 2.2 + _noise(rng, 0.2), drift)
            emit("temperature", t, 65 + _noise(rng, 0.8), drift)
            emit("current", t, 13 + _noise(rng, 0.4), drift)
            if rng.random() < 0.03:                # heavy duplicate resends
                emit("vibration", t + rng.randint(2, 9), 2.2 + _noise(rng, 0.2), drift)

    elif device == "C-201":
        change = t0 + 18 * DAY                     # 换型 CX-5 -> CX-7
        exc = (t0 + 25 * DAY + 14 * H, t0 + 25 * DAY + 17 * H)
        for t in range(t0, end, INTERVAL):
            new_model = t >= change
            vib = (2.2 if new_model else 3.0) + _noise(rng, 0.25 if new_model else 0.3)
            temp = 70 + _noise(rng, 1.5)
            if exc[0] <= t < exc[1]:               # 3h temperature excursion
                temp = 88 + 6 * (t - exc[0]) / (exc[1] - exc[0]) + _noise(rng, 0.5)
            emit("vibration", t, vib)
            emit("temperature", t, temp)
            emit("current", t, 20 + _noise(rng, 0.8))
    return pts


def seed(db_path: str, days: int = 40) -> None:
    rng = random.Random(42)
    t_now = int(time.time())
    t0 = (t_now - days * DAY) // H * H
    end = t0 + days * DAY

    conn = db.connect(db_path)
    db.init_db(conn)
    rules_mod.ensure_default_rules(conn)

    cut = t0 + 20 * DAY
    conn.executemany(
        "INSERT INTO devices(device_id, name, line, expected_interval_s, installed_at)"
        " VALUES (?,?,?,?,?)",
        [("P-101", "冷却泵 101", "L1", INTERVAL, t0),
         ("P-101R", "冷却泵 101R", "L1", INTERVAL, cut - 2 * H),
         ("P-102", "冷却泵 102", "L1", INTERVAL, t0),
         ("C-201", "空压机 201", "L2", INTERVAL, t0)])
    conn.executemany(
        "INSERT INTO asset_links(asset_id, device_id, valid_from, valid_to) VALUES (?,?,?,?)",
        [("PUMP-A", "P-101", t0, cut), ("PUMP-A", "P-101R", cut, None),
         ("PUMP-B", "P-102", t0, None), ("COMP-1", "C-201", t0, None)])
    conn.executemany(
        "INSERT INTO device_model_events(device_id, model, effective_from, note) VALUES (?,?,?,?)",
        [("P-101", "PU-3000", t0, "initial"), ("P-101R", "PU-3100", cut - 2 * H, "replacement unit"),
         ("P-102", "PU-3000", t0, "initial"), ("C-201", "CX-5", t0, "initial"),
         ("C-201", "CX-7", t0 + 18 * DAY, "换型: 转子升级, 振动基线下移")])
    conn.executemany(
        "INSERT INTO work_orders(device_id, opened_at, closed_at, kind, summary, status)"
        " VALUES (?,?,?,?,?,?)",
        [("P-101R", t0 + 33 * DAY, None, "repair", "轴承异响检查", "open"),
         ("P-102", t0 + 20 * DAY + 8 * H, t0 + 20 * DAY + 14 * H, "maintenance",
          "计划保养", "closed"),
         ("C-201", t0 + 25 * DAY + 18 * H, t0 + 25 * DAY + 20 * H, "inspection",
          "温度异常核查", "closed")])
    conn.executemany(
        "INSERT INTO downtime_records(device_id, start_ts, end_ts, reason) VALUES (?,?,?,?)",
        [("P-102", t0 + 20 * DAY + 8 * H, t0 + 20 * DAY + 14 * H, "计划保养停机"),
         ("P-101R", t0 + 35 * DAY + 10 * H, t0 + 35 * DAY + 12 * H, "联轴器对中")])
    conn.commit()

    # ---- live ingestion in 4-day chunks per device, in time order ----
    all_pts = {d: gen_device_points(rng, d, t0, days)
               for d in ("P-101", "P-101R", "P-102", "C-201")}
    chunk = 4 * DAY
    n_live = 0
    for cs in range(t0, end, chunk):
        for d, pts in all_pts.items():
            batch = [p for p in pts if cs <= p["ts"] < cs + chunk]
            if batch:
                pipeline.ingest_points(conn, batch, f"gw-{d}", "live", cs + chunk + 8)
                n_live += 1
    print(f"live ingestion: {n_live} batches, data_version={db.current_data_version(conn)}")

    # ---- late data (backfill) ----
    # P-102's day-15 10:00-12:00 outage points finally arrive (backfill points
    # are never drift-corrected: arrival lag is not clock drift)
    p102_late = [{"device_id": "P-102", "metric": m, "ts": t,
                  "value": v + rng.gauss(0, s), "reported_at": end + H}
                 for t in range(t0 + 15 * DAY + 10 * H, t0 + 15 * DAY + 12 * H, INTERVAL)
                 for m, v, s in (("vibration", 2.2, 0.2), ("temperature", 65, 0.8),
                                 ("current", 13, 0.4))]
    # regenerate the lost hour of P-101 (it was dropped from live on purpose)
    p101_late = [{"device_id": "P-101", "metric": m, "ts": t,
                  "value": v + rng.gauss(0, s), "reported_at": end + 2 * H}
                 for t in range(t0 + 5 * DAY + 6 * H, t0 + 5 * DAY + 7 * H, INTERVAL)
                 for m, v, s in (("vibration", 2.3, 0.1), ("temperature", 62, 0.5),
                                 ("current", 12, 0.3))]
    r1 = pipeline.ingest_points(conn, p102_late, "gw-P-102", "backfill", end + H)
    r2 = pipeline.ingest_points(conn, p101_late, "gw-P-101", "backfill", end + 2 * H)
    print(f"backfill P-102 day15 10-12h: batch {r1['batch_id']}, "
          f"affected windows={r1['affected_windows']}")
    print(f"backfill P-101 day5 06-07h:  batch {r2['batch_id']}, "
          f"affected windows={r2['affected_windows']}")

    # ---- rule change: stricter vibration z + warn score ----
    rv1, cfg = rules_mod.get_active_rules(conn)
    cfg2 = dict(cfg)
    cfg2["metrics"] = {**cfg["metrics"],
                       "vibration": {**cfg["metrics"]["vibration"], "crit_z": 2.6}}
    cfg2["alert_score_warn"] = 85.0
    out = pipeline.apply_rule_change(conn, cfg2)
    print(f"rule change: v{rv1} -> v{out['rule_version']}, "
          f"windows recomputed={out['windows_recomputed']}")

    # ---- summary ----
    for d in db.rows(conn, "SELECT device_id FROM devices"):
        dev = d["device_id"]
        snap = db.row(conn, "SELECT health_score, coverage FROM health_snapshots"
                           " WHERE device_id=? AND superseded_by IS NULL"
                           " AND health_score IS NOT NULL"
                           " ORDER BY window_start DESC LIMIT 1", (dev,))
        nal = db.row(conn, "SELECT COUNT(*) n FROM alerts WHERE device_id=?"
                          " AND superseded_by IS NULL AND status!='resolved'", (dev,))
        rul = db.row(conn, "SELECT rul_hours FROM rul_estimates WHERE device_id=?"
                          " AND superseded_by IS NULL ORDER BY as_of DESC LIMIT 1", (dev,))
        qual = db.rows(conn, "SELECT quality, COUNT(*) n FROM cleaned_points"
                            " WHERE device_id=? GROUP BY quality", (dev,))
        print(f"{dev}: health={snap['health_score']} coverage={snap['coverage']} "
              f"open_alerts={nal['n']} rul_h={rul['rul_hours'] if rul else None} "
              f"quality={ {q['quality']: q['n'] for q in qual} }")
    nlog = db.row(conn, "SELECT COUNT(*) n FROM recompute_log")
    print(f"recompute_log rows: {nlog['n']}")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dha.db")
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--fresh", action="store_true", help="delete db file first")
    args = ap.parse_args()
    if args.fresh:
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(args.db + suffix)
            except FileNotFoundError:
                pass
    seed(args.db, args.days)


if __name__ == "__main__":
    main()
