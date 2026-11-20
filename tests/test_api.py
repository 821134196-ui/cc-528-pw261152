"""API smoke test: ingest -> overview -> timeline -> evidence -> disposition -> diff."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, rules as rules_mod  # noqa: E402
from app import api  # noqa: E402

from conftest import make_rules  # noqa: E402

H = 3600


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = str(tmp_path / "api.db")
    monkeypatch.setattr(api, "DB_PATH", path)
    api.reset_connection()
    c = db.connect(path)
    db.init_db(c)
    c.execute("INSERT INTO devices(device_id, name, expected_interval_s, installed_at)"
              " VALUES ('D1','D1',60,0)")
    rules_mod.create_rule_version(c, make_rules(), activate=True)
    c.commit()
    c.close()
    from fastapi.testclient import TestClient
    with TestClient(api.app) as tc:
        yield tc
    api.reset_connection()


def _points():
    pts = []
    for t in range(0, 6 * H, 60):
        if 2 * H <= t < 3 * H:
            continue  # hole to be backfilled later
        v = 9.0 if 4 * H <= t < 5 * H else (1.9 if (t // 60) % 2 == 0 else 2.1)
        pts.append({"device_id": "D1", "metric": "vibration", "ts": t, "value": v,
                    "reported_at": t + 5})
    return pts


def test_full_flow(client):
    r = client.post("/api/ingest", json={"points": _points(), "source": "test",
                                         "kind": "live", "arrived_at": 6 * H + 5})
    assert r.status_code == 200
    dv1 = r.json()["data_version"]

    ov = client.get("/api/overview").json()
    assert ov["devices"][0]["device_id"] == "D1"
    assert ov["devices"][0]["open_alerts"] > 0

    tl = client.get("/api/devices/D1/timeline", params={"start": 0, "end": 6 * H}).json()
    assert tl["snapshots"] and tl["alerts"]

    crit = [a for a in tl["alerts"] if a["rule_id"] == "metric_z_crit"]
    assert crit and crit[0]["window_start"] == 4 * H

    ev = client.get("/api/devices/D1/evidence", params={"window_start": 4 * H}).json()
    assert ev["alerts"] and ev["snapshot"]["metrics"]["vibration"]["max"] == 9.0

    # disposition flow
    aid = crit[0]["id"]
    r = client.post(f"/api/alerts/{aid}/disposition",
                    json={"status": "ack", "disposition": "monitoring", "note": "watching"})
    assert r.status_code == 200 and r.json()["status"] == "ack"

    # late backfill of the hole -> only affected windows recomputed
    hole = [{"device_id": "D1", "metric": "vibration", "ts": t, "value": 2.0}
            for t in range(2 * H, 3 * H, 60)]
    r = client.post("/api/ingest", json={"points": hole, "source": "test",
                                         "kind": "backfill", "arrived_at": 12 * H})
    dv2 = r.json()["data_version"]
    assert dv2 == dv1 + 1

    diff = client.get("/api/version-diff",
                      params={"device_id": "D1", "v1": dv1, "v2": dv2}).json()
    changed = {w["window_start"] for w in diff["changed_windows"]}
    assert 2 * H in changed                       # the backfilled window itself
    assert all(w <= 5 * H for w in changed)       # nothing beyond baseline reach

    b = client.get("/api/batches").json()["batches"]
    assert len(b) == 2
    assert b[1]["kind"] == "backfill"
    assert b[1]["recompute"] and all(x["reason"] == "late_data" for x in b[1]["recompute"])

    # disposition survived the backfill recompute
    a = client.get(f"/api/alerts/{aid}").json()
    assert a["status"] == "ack" and a["disposition"] == "monitoring"
