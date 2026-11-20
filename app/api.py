"""FastAPI surface: devices, timelines, evidence, versions/diff, alerts, batches."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import alerts, db, merge, pipeline

DB_PATH = os.environ.get("DHA_DB", str(Path(__file__).resolve().parent.parent / "dha.db"))

app = FastAPI(title="Device Health Analysis")
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = db.connect(DB_PATH)
        db.init_db(_conn)
    return _conn


def reset_connection() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------- payloads ----------

class IngestPoint(BaseModel):
    device_id: str
    metric: str
    ts: int
    value: float
    reported_at: int | None = None   # per-point arrival; defaults to batch arrived_at


class IngestRequest(BaseModel):
    points: list[IngestPoint]
    source: str = "api"
    kind: str = "live"
    arrived_at: int


class DispositionRequest(BaseModel):
    status: str
    disposition: str | None = None
    note: str | None = None


class RuleChangeRequest(BaseModel):
    config: dict


# ---------- overview / devices ----------

@app.get("/api/overview")
def overview():
    c = conn()
    out = []
    for d in db.rows(c, "SELECT * FROM devices ORDER BY device_id"):
        dev = d["device_id"]
        snap = db.row(c, "SELECT * FROM health_snapshots WHERE device_id=? AND superseded_by IS NULL"
                         " AND health_score IS NOT NULL"
                         " ORDER BY window_start DESC LIMIT 1", (dev,))
        open_alerts = db.row(c, "SELECT COUNT(*) AS n, MAX(severity) AS sev FROM alerts"
                               " WHERE device_id=? AND superseded_by IS NULL AND status!='resolved'",
                             (dev,))
        rul = db.row(c, "SELECT * FROM rul_estimates WHERE device_id=? AND superseded_by IS NULL"
                        " ORDER BY as_of DESC LIMIT 1", (dev,))
        asset = db.row(c, "SELECT asset_id FROM asset_links WHERE device_id=? ORDER BY valid_from DESC LIMIT 1", (dev,))
        out.append({
            **d,
            "asset_id": asset["asset_id"] if asset else None,
            "health_score": snap["health_score"] if snap else None,
            "coverage": snap["coverage"] if snap else None,
            "as_of": snap["window_end"] if snap else None,
            "open_alerts": open_alerts["n"],
            "worst_severity": open_alerts["sev"],
            "rul_hours": rul["rul_hours"] if rul else None,
            "data_version": snap["data_version"] if snap else None,
            "rule_version": snap["rule_version"] if snap else None,
        })
    return {"devices": out, "data_version": db.current_data_version(c)}


@app.get("/api/devices/{device_id}/timeline")
def timeline(device_id: str, start: int, end: int):
    c = conn()
    snaps = db.rows(c, "SELECT window_start, window_end, health_score, coverage, data_version,"
                      " rule_version FROM health_snapshots WHERE device_id=? AND superseded_by IS NULL"
                      " AND window_start>=? AND window_start<? ORDER BY window_start",
                    (device_id, start, end))
    al = db.rows(c, "SELECT id, rule_id, metric, window_start, severity, status, disposition,"
                   " data_version, rule_version FROM alerts WHERE device_id=? AND superseded_by IS NULL"
                   " AND window_start>=? AND window_start<? ORDER BY window_start",
                 (device_id, start, end))
    downs = db.rows(c, "SELECT * FROM downtime_records WHERE device_id=? AND start_ts<? AND"
                      " COALESCE(end_ts, ?)>?", (device_id, end, end, start))
    wos = db.rows(c, "SELECT * FROM work_orders WHERE device_id=? AND opened_at<? AND"
                    " COALESCE(closed_at, ?)>?", (device_id, end, end, start))
    models = db.rows(c, "SELECT * FROM device_model_events WHERE device_id=? ORDER BY effective_from",
                     (device_id,))
    quality = db.rows(c, "SELECT quality, COUNT(*) AS n FROM cleaned_points WHERE device_id=?"
                        " AND ts>=? AND ts<? GROUP BY quality", (device_id, start, end))
    rul_rows = db.rows(c, "SELECT as_of, rul_hours, data_version, rule_version FROM rul_estimates"
                         " WHERE device_id=? AND superseded_by IS NULL AND as_of>=? AND as_of<?"
                         " ORDER BY as_of", (device_id, start, end))
    return {"snapshots": snaps, "alerts": al, "downtime": downs, "work_orders": wos,
            "model_events": models, "quality": quality, "rul": rul_rows}


@app.get("/api/devices/{device_id}/points")
def points(device_id: str, metric: str, start: int, end: int):
    c = conn()
    return {"points": db.rows(c, "SELECT ts, value, quality, drift_s FROM cleaned_points"
                                " WHERE device_id=? AND metric=? AND ts>=? AND ts<? ORDER BY ts",
                              (device_id, metric, start, end))}


@app.get("/api/devices/{device_id}/evidence")
def evidence(device_id: str, window_start: int):
    """Anomaly evidence for one window: current alerts with full evidence payload
    plus the window's metric detail from its snapshot."""
    c = conn()
    al = [dict(a, evidence=json.loads(a["evidence_json"])) for a in db.rows(
        c, "SELECT * FROM alerts WHERE device_id=? AND window_start=? AND superseded_by IS NULL",
        (device_id, window_start))]
    snap = db.row(c, "SELECT * FROM health_snapshots WHERE device_id=? AND window_start=?"
                    " AND superseded_by IS NULL", (device_id, window_start))
    return {"alerts": al,
            "snapshot": {**snap, "metrics": json.loads(snap["metrics_json"])} if snap else None}


# ---------- alerts & disposition ----------

@app.get("/api/alerts")
def list_alerts(status: str | None = None, device_id: str | None = None):
    c = conn()
    sql = ("SELECT id, device_id, rule_id, metric, window_start, window_end, severity, status,"
           " disposition, disposition_note, data_version, rule_version, updated_at FROM alerts"
           " WHERE superseded_by IS NULL")
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    sql += " ORDER BY window_start DESC LIMIT 500"
    return {"alerts": db.rows(c, sql, params)}


@app.get("/api/alerts/{alert_id}")
def alert_detail(alert_id: int):
    c = conn()
    a = db.row(c, "SELECT * FROM alerts WHERE id=?", (alert_id,))
    if not a:
        raise HTTPException(404, "alert not found")
    a["evidence"] = json.loads(a.pop("evidence_json"))
    return a


@app.post("/api/alerts/{alert_id}/disposition")
def set_disposition(alert_id: int, req: DispositionRequest):
    try:
        return alerts.set_disposition(conn(), alert_id, req.status, req.disposition, req.note)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- assets: cross-device merge ----------

@app.get("/api/assets")
def assets():
    c = conn()
    out: dict[str, list] = {}
    for l in db.rows(c, "SELECT * FROM asset_links ORDER BY asset_id, valid_from"):
        out.setdefault(l["asset_id"], []).append(l)
    return {"assets": out}


@app.get("/api/assets/{asset_id}/merged")
def asset_merged(asset_id: str, metric: str, start: int, end: int):
    c = conn()
    return {"series": merge.merged_series(c, asset_id, metric, start, end),
            "health": merge.merged_health(c, asset_id, start, end),
            "links": merge.asset_devices(c, asset_id)}


# ---------- versions & diff ----------

def _view(rows: list[dict], key_fn, dv: int, rv: int) -> dict:
    """Latest visible row per key at (data_version<=dv, rule_version<=rv)."""
    best: dict = {}
    for r in rows:
        if r["data_version"] <= dv and r["rule_version"] <= rv:
            k = key_fn(r)
            cur = best.get(k)
            if cur is None or (r["data_version"], r["rule_version"], r["id"]) > \
                              (cur["data_version"], cur["rule_version"], cur["id"]):
                best[k] = r
    return best


@app.get("/api/versions")
def versions():
    c = conn()
    return {
        "data_version": db.current_data_version(c),
        "batches": db.rows(c, "SELECT batch_id, arrived_at, source, kind, data_version, n_points,"
                             " min_ts, max_ts FROM ingest_batches ORDER BY batch_id"),
        "rule_versions": db.rows(c, "SELECT rule_version, config_json, created_at, active"
                                   " FROM rule_versions ORDER BY rule_version"),
    }


@app.get("/api/version-diff")
def version_diff(device_id: str, v1: int, v2: int, r1: int = 10**9, r2: int = 10**9):
    """Diff health snapshots / alerts / RUL of one device between two
    (data_version, rule_version) views. Defaults: any rule version."""
    c = conn()
    snaps = db.rows(c, "SELECT * FROM health_snapshots WHERE device_id=?", (device_id,))
    al = db.rows(c, "SELECT * FROM alerts WHERE device_id=?", (device_id,))
    ruls = db.rows(c, "SELECT * FROM rul_estimates WHERE device_id=?", (device_id,))

    s1 = _view(snaps, lambda r: r["window_start"], v1, r1)
    s2 = _view(snaps, lambda r: r["window_start"], v2, r2)
    changed_windows = []
    for w in sorted(set(s1) | set(s2)):
        a, b = s1.get(w), s2.get(w)
        sa = a["health_score"] if a else None
        sb_ = b["health_score"] if b else None
        if sa != sb_ or (a is None) != (b is None):
            changed_windows.append({"window_start": w, "from": sa, "to": sb_,
                                    "delta": (round(sb_ - sa, 2) if sa is not None and sb_ is not None else None)})

    a1 = _view(al, lambda r: (r["rule_id"], r["metric"], r["window_start"]), v1, r1)
    a2 = _view(al, lambda r: (r["rule_id"], r["metric"], r["window_start"]), v2, r2)
    added = [{"rule_id": k[0], "metric": k[1], "window_start": k[2], "severity": a2[k]["severity"]}
             for k in sorted(a2) if k not in a1]
    removed = [{"rule_id": k[0], "metric": k[1], "window_start": k[2], "severity": a1[k]["severity"]}
               for k in sorted(a1) if k not in a2]

    def latest_rul(view):
        cur = None
        for r in ruls:
            if r["data_version"] <= (v1 if view == 1 else v2) and r["rule_version"] <= (r1 if view == 1 else r2):
                if cur is None or r["as_of"] > cur["as_of"]:
                    cur = r
        return {"as_of": cur["as_of"], "rul_hours": cur["rul_hours"]} if cur else None

    return {"device_id": device_id, "view1": {"data_version": v1, "rule_version": r1},
            "view2": {"data_version": v2, "rule_version": r2},
            "changed_windows": changed_windows, "alerts_added": added, "alerts_removed": removed,
            "rul1": latest_rul(1), "rul2": latest_rul(2)}


# ---------- batches / recompute proof ----------

@app.get("/api/batches")
def batches():
    c = conn()
    out = []
    for b in db.rows(c, "SELECT * FROM ingest_batches ORDER BY batch_id"):
        rec = db.rows(c, "SELECT device_id, reason, COUNT(*) AS windows, MIN(window_start) AS w_from,"
                        " MAX(window_end) AS w_to FROM recompute_log WHERE batch_id=? GROUP BY device_id, reason",
                      (b["batch_id"],))
        out.append({**b, "recompute": rec})
    return {"batches": out}


@app.get("/api/batches/{batch_id}/recompute")
def batch_recompute(batch_id: int):
    c = conn()
    return {"log": db.rows(c, "SELECT * FROM recompute_log WHERE batch_id=? ORDER BY device_id, window_start",
                           (batch_id,))}


# ---------- ingest & rules ----------

@app.post("/api/ingest")
def ingest(req: IngestRequest):
    if req.kind not in ("live", "backfill"):
        raise HTTPException(400, "kind must be live|backfill")
    return pipeline.ingest_points(conn(), [p.model_dump() for p in req.points],
                                  req.source, req.kind, req.arrived_at)


@app.post("/api/rules")
def rule_change(req: RuleChangeRequest):
    return pipeline.apply_rule_change(conn(), req.config)


@app.get("/api/rul")
def rul_history(device_id: str):
    c = conn()
    return {"rul": db.rows(c, "SELECT as_of, rul_hours, detail_json, data_version, rule_version"
                             " FROM rul_estimates WHERE device_id=? AND superseded_by IS NULL"
                             " ORDER BY as_of", (device_id,))}


# ---------- frontend ----------

WEB = Path(__file__).resolve().parent.parent / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


if WEB.exists():
    app.mount("/static", StaticFiles(directory=WEB), name="static")
