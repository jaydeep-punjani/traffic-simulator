import time

import pytest
from fastapi.testclient import TestClient

from trafficsim.api.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(runs_dir=str(tmp_path))) as c:
        yield c


def test_static_and_metadata(client):
    r = client.get("/")
    assert r.status_code == 200 and "trafficsim" in r.text and "/static/app.js" in r.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    meta = client.get("/api/meta").json()
    assert set(meta["verticals"]) == {"streaming", "ecom", "gaming"}
    assert "flash_sale" in meta["event_types"] and meta["default_platform_mix"]["gaming"]
    ids = [s["id"] for s in client.get("/api/scenarios").json()]
    assert "stress_100k" in ids and "ecom_flash_sale" in ids
    sc = client.get("/api/scenarios/ecom_flash_sale").json()
    assert sc["raw"]["events"][0]["type"] == "flash_sale" and "flash_sale" in sc["yaml"]
    assert client.get("/api/scenarios/nope").status_code == 404
    assert client.get("/api/runs/current").status_code == 404


def test_validate(client):
    ok = client.post("/api/validate", json={"scenario": "baseline_ecom", "overrides": {"traffic.base_concurrency": 1000}})
    assert ok.status_code == 200 and ok.json()["peak"] <= 1000
    bad = client.post("/api/validate", json={"yaml": "name: x\nverticals: {gaming: {platforms: {ios: 1}}}"})
    assert bad.status_code == 422


def test_run_lifecycle_over_rest_and_websocket(client, tmp_path):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        r = client.post(
            "/api/runs",
            json={"scenario": "mixed_all", "dry_run": True, "overrides": {"traffic.base_concurrency": 600, "workers": 2, "speed": 120}},
        )
        assert r.status_code == 200, r.text
        rid = r.json()["run_id"]
        assert client.post("/api/runs", json={"scenario": "mixed_all", "dry_run": True}).status_code == 409
        points = []
        deadline = time.time() + 30
        while len(points) < 3 and time.time() < deadline:
            m = ws.receive_json()
            if m["type"] == "point" and m["run_id"] == rid:
                points.append(m)
        assert len(points) >= 3
        assert points[-1]["active"] > 0 and points[-1]["eps"] > 0 and "funnels" in points[-1]

        assert client.post("/api/runs/current/pause").json()["state"] == "paused"
        assert client.post("/api/runs/current/resume").json()["state"] == "running"
        assert client.post("/api/runs/current/speed", json={"speed": 30}).json()["speed"] == 30
        ov = client.post("/api/runs/current/events", json={"type": "flash_sale", "magnitude": 2}).json()
        assert ov["vertical"] == "ecom" and ov["magnitude"] == 2
        assert client.post("/api/runs/current/events", json={"type": "nope"}).status_code in (400, 422)
        assert client.get("/api/runs/current/export/timeseries.csv").status_code == 200

        client.post("/api/runs/current/stop")
        finished = None
        deadline = time.time() + 60
        while finished is None and time.time() < deadline:
            m = ws.receive_json()
            if m["type"] == "finished":
                finished = m
        assert finished and finished["summary"]["total_events"] > 0
    st = client.get("/api/runs/current").json()
    assert st["state"] == "finished"
    files = {f for r in client.get("/api/runs").json() if r["id"] == rid for f in r["files"]}
    assert {"summary.json", "timeseries.csv"} <= files
    assert client.get(f"/api/runs/{rid}/files/summary.json").json()["run_id"] == rid
    assert client.get(f"/api/runs/{rid}/files/..%2F..%2Fetc%2Fpasswd").status_code == 404
