"""SQLite storage layer: schema, connection helpers, data-version clock."""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS clock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  data_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  line TEXT,
  expected_interval_s INTEGER NOT NULL DEFAULT 300,
  installed_at INTEGER
);

-- physical asset continuity: which device serves an asset over which interval
CREATE TABLE IF NOT EXISTS asset_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id TEXT NOT NULL,
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  valid_from INTEGER NOT NULL,
  valid_to INTEGER                    -- NULL = open-ended
);

-- 换型: model/config change that resets baselines
CREATE TABLE IF NOT EXISTS device_model_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  model TEXT NOT NULL,
  effective_from INTEGER NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
  arrived_at INTEGER NOT NULL,
  source TEXT,
  kind TEXT NOT NULL DEFAULT 'live',  -- live | backfill
  data_version INTEGER NOT NULL,
  n_points INTEGER NOT NULL,
  min_ts INTEGER,
  max_ts INTEGER
);

CREATE TABLE IF NOT EXISTS sensor_readings_raw (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES ingest_batches(batch_id),
  device_id TEXT NOT NULL,
  metric TEXT NOT NULL,
  ts INTEGER NOT NULL,                -- sensor-reported ts (epoch s)
  value REAL NOT NULL,
  reported_at INTEGER NOT NULL        -- arrival ts (for clock-drift estimation)
);
CREATE INDEX IF NOT EXISTS idx_raw_dmt ON sensor_readings_raw(device_id, metric, ts);

CREATE TABLE IF NOT EXISTS cleaned_points (
  device_id TEXT NOT NULL,
  metric TEXT NOT NULL,
  ts INTEGER NOT NULL,                -- corrected ts
  value REAL NOT NULL,
  quality TEXT NOT NULL,              -- ok | dup_resolved | gap_filled | drift_corrected
  drift_s INTEGER NOT NULL DEFAULT 0,
  data_version INTEGER NOT NULL,      -- data version at which this cleaned state was built
  PRIMARY KEY (device_id, metric, ts)
);

CREATE TABLE IF NOT EXISTS rule_versions (
  rule_version INTEGER PRIMARY KEY AUTOINCREMENT,
  config_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS health_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  window_end INTEGER NOT NULL,
  health_score REAL,                  -- NULL when coverage too low to score
  coverage REAL NOT NULL,
  metrics_json TEXT NOT NULL,
  data_version INTEGER NOT NULL,
  rule_version INTEGER NOT NULL,
  superseded_by INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hs_dw ON health_snapshots(device_id, window_start);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  metric TEXT,
  window_start INTEGER NOT NULL,
  window_end INTEGER NOT NULL,
  severity TEXT NOT NULL,             -- info | warn | crit
  evidence_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',-- open | ack | resolved
  disposition TEXT,                   -- fixed | false_positive | monitoring | parts_ordered
  disposition_note TEXT,
  data_version INTEGER NOT NULL,
  rule_version INTEGER NOT NULL,
  superseded_by INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rul_estimates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  as_of INTEGER NOT NULL,             -- window_end this estimate is made at
  rul_hours REAL,                     -- NULL = no degradation trend
  method TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  data_version INTEGER NOT NULL,
  rule_version INTEGER NOT NULL,
  superseded_by INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rul_da ON rul_estimates(device_id, as_of);

CREATE TABLE IF NOT EXISTS recompute_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER,                   -- NULL for rule-change recompute
  device_id TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  window_end INTEGER NOT NULL,
  reason TEXT NOT NULL,               -- new_data | late_data | rule_change
  data_version INTEGER NOT NULL,
  rule_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rl_b ON recompute_log(batch_id);

CREATE TABLE IF NOT EXISTS work_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  opened_at INTEGER NOT NULL,
  closed_at INTEGER,
  kind TEXT NOT NULL,                 -- repair | maintenance | inspection
  summary TEXT,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS downtime_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  start_ts INTEGER NOT NULL,
  end_ts INTEGER,
  reason TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT data_version FROM clock WHERE id=1").fetchone()
    if row is None:
        conn.execute("INSERT INTO clock(id, data_version) VALUES (1, 0)")
    conn.commit()


def bump_data_version(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE clock SET data_version = data_version + 1 WHERE id=1")
    return conn.execute("SELECT data_version FROM clock WHERE id=1").fetchone()[0]


def current_data_version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT data_version FROM clock WHERE id=1").fetchone()[0]


def now_ts() -> int:
    return int(time.time())


def rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params=()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None
