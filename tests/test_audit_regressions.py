"""Regression tests for issues found in the 2026-11 audit (see docs/audit-2026-11.md)."""

import gzip
import json
import random
import threading
import time
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trafficsim.api.app import create_app
from trafficsim.clock import SimClock
from trafficsim.config import (
    Destinations,
    expand_env,
    load_destinations,
    load_scenario,
    resolve_catalog_path,
    resolve_scenario_path,
    safe_load_strict,
)
from trafficsim.contexts import build_context
from trafficsim.emitter import SimClient
from trafficsim.events import Overlay, effects_for
from trafficsim.identity import UserPool, make_user
from trafficsim.journeys import Catalog, Journey, State
from trafficsim.mock_collector import MAX_INFLATED, Collector, make_app
from trafficsim.patterns import build_pattern
from trafficsim.planner import TrafficPlanner
from trafficsim.runner import timestamp_warnings
from trafficsim.scheduler import Scheduler
from trafficsim.stats import StatsAggregator

START = 1_767_225_600.0


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(runs_dir=str(tmp_path))) as c:
        yield c


# ---------------------------------------------------------------- security: API


def test_cross_origin_writes_are_rejected(client):
    body = {"scenario": "baseline_ecom"}
    r = client.post("/api/validate", json=body, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/validate", json=body, headers={"Origin": "null"})
    assert r.status_code == 403
    assert client.post("/api/validate", json=body, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/validate", json=body).status_code == 200  # non-browser clients send no Origin


def test_security_headers_and_csp(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "cdn." not in r.text  # Chart.js is vendored, nothing loads from third parties
    assert client.get("/static/vendor/chart.umd.js").status_code == 200


def test_websocket_rejects_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}) as ws:
        ws.receive_json()
    assert exc.value.code == 1008


def test_scenario_ids_and_catalog_refs_cannot_be_paths(client):
    assert client.get("/api/scenarios/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    assert client.get("/api/scenarios/bad.id").status_code == 400
    with pytest.raises(ValueError):
        resolve_scenario_path("../pyproject", allow_path=False)
    with pytest.raises(ValueError):
        resolve_catalog_path("ecom", "/etc/passwd", allow_path=False)
    y = "name: x\nverticals:\n  ecom: {catalog: /etc/passwd}\n"
    assert client.post("/api/validate", json={"yaml": y}).status_code == 422


def test_yaml_aliases_and_bad_yaml_rejected(client):
    bomb = "a: &a [x, x]\nb: [*a, *a]\n"
    with pytest.raises(yaml.YAMLError):
        safe_load_strict(bomb)
    assert client.post("/api/validate", json={"yaml": bomb}).status_code == 400
    assert client.post("/api/validate", json={"yaml": "- just\n- a list\n"}).status_code == 400
    assert client.post("/api/validate", json={"yaml": "x" * (256 * 1024 + 1)}).status_code == 422


def test_validation_errors_do_not_echo_input(client):
    r = client.post("/api/validate", json={"yaml": "name: x\nspeed: -5\nsecret_field: hunter2\n"})
    assert r.status_code == 422 and "hunter2" not in r.text


def test_series_query_is_bounded(client):
    assert client.get("/api/runs/current/series?last=0").status_code == 422
    assert client.get("/api/runs/current/series?last=999999").status_code == 422


def test_control_endpoints_conflict_when_no_active_run(client):
    r = client.post("/api/runs", json={"scenario": "baseline_ecom", "dry_run": True, "overrides": {"traffic.base_concurrency": 50}})
    assert r.status_code == 200
    client.post("/api/runs/current/stop")
    deadline = time.time() + 60
    while client.get("/api/runs/current").json()["state"] != "finished" and time.time() < deadline:
        time.sleep(0.2)
    assert client.get("/api/runs/current").json()["state"] == "finished"
    for path, body in [("pause", None), ("resume", None), ("speed", {"speed": 10}), ("events", {"type": "flash_sale"})]:
        assert client.post(f"/api/runs/current/{path}", json=body).status_code == 409, path


def test_concurrent_starts_create_one_run(tmp_path):
    with TestClient(create_app(runs_dir=str(tmp_path))) as c:
        body = {"scenario": "baseline_ecom", "dry_run": True, "overrides": {"traffic.base_concurrency": 50}}
        codes: list[int] = []

        def go():
            codes.append(c.post("/api/runs", json=body).status_code)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(codes) == [200, 409, 409, 409]
        c.post("/api/runs/current/stop")


# ---------------------------------------------------------------- security: mock collector


def _post(app, path, body, headers=()):
    """Drive the raw ASGI collector app without a server."""
    import asyncio

    out: dict = {}
    chunks = [body[i : i + 65536] for i in range(0, len(body), 65536)] or [b""]

    async def receive():
        c = chunks.pop(0)
        return {"type": "http.request", "body": c, "more_body": bool(chunks)}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]

    scope = {"type": "http", "method": "POST", "path": path, "headers": list(headers)}
    asyncio.run(app(scope, receive, send))
    return out["status"]


def test_collector_bounds_bodies_and_gzip():
    col = Collector()
    app = make_app(col)
    ok = json.dumps({"batch": [{"type": "track", "messageId": "1"}]}).encode()
    assert _post(app, "/v1/batch", gzip.compress(ok), [(b"content-encoding", b"gzip")]) == 200
    bomb = gzip.compress(b"\0" * (MAX_INFLATED + 1024))
    assert len(bomb) < 4 * 1024 * 1024
    assert _post(app, "/v1/batch", bomb, [(b"content-encoding", b"gzip")]) == 400
    assert _post(app, "/v1/batch", b"not gzip", [(b"content-encoding", b"gzip")]) == 400
    assert _post(app, "/v1/batch", b"x" * (4 * 1024 * 1024 + 1)) == 413
    st = col.stats()
    assert st["events"] == 1 and st["bad_requests"] == 3
    lock = col.lock
    col.reset()
    assert col.lock is lock and col.stats()["events"] == 0


# ---------------------------------------------------------------- config


def test_env_default_applies_when_unset_or_empty(monkeypatch):
    monkeypatch.setenv("TS_EMPTY", "")
    monkeypatch.delenv("TS_UNSET", raising=False)
    assert expand_env("${TS_EMPTY:-d}") == "d"
    assert expand_env("${TS_UNSET:-d}") == "d"
    assert expand_env("${TS_UNSET}") == ""


def test_destination_host_must_be_http_url(tmp_path):
    with pytest.raises(ValidationError):
        Destinations(host="ftp://x")
    f = tmp_path / "d.yaml"
    f.write_text("host: ''\n")
    with pytest.raises(ValueError):
        load_destinations(str(f))
    assert load_destinations(str(f), host_override="http://localhost:1").host == "http://localhost:1"


def test_write_keys_are_fully_redacted():
    d = Destinations(default_write_key="abcdefgh12345678", sources={"ecom": "zz-secret-zz"})
    text = json.dumps(d.redacted())
    assert "abcd" not in text and "5678" not in text and "secret" not in text


def test_iso_start_at_and_bounds(make_scenario):
    sc = make_scenario(start_at="2026-01-01T00:00:00Z")
    assert sc.start_at.startswith("2026-01-01")
    raw = yaml.safe_load("start_at: 2026-01-01T00:00:00Z\n")  # YAML parses this to a datetime
    assert make_scenario(start_at=raw["start_at"]).start_at.startswith("2026-01-01")
    for bad in [{"start_at": "tomorrow-ish"}, {"speed": 100000}, {"duration": "0s"}]:
        with pytest.raises(ValidationError):
            make_scenario(**bad)
    with pytest.raises(ValueError):
        Overlay({"type": "flash_sale", "magnitude": 1e6}, 0)
    with pytest.raises(ValueError):
        Overlay({"type": "flash_sale", "hold": "400d"}, 0)


def test_timestamp_warning_only_when_running_ahead(make_scenario):
    sc = make_scenario(speed=60)
    assert timestamp_warnings(sc, START, START + 3600, now=START)  # 1h of sim in 1min: ~59min ahead
    assert not timestamp_warnings(sc, START - 86400, START - 86400 + 3600, now=START)  # historical
    assert not timestamp_warnings(make_scenario(speed=1), START, START + 3600, now=START)


# ---------------------------------------------------------------- simulation model


def test_diurnal_handles_fractional_and_negative_offsets():
    di = build_pattern({"type": "diurnal", "profile": "ecom"}, 0)
    for tz in (-9.5, -3, 0, 5.5, 5.75, 13):
        for t in (0, 3599.9, 86399.99, 86400 * 3 + 0.5):
            assert 0 < di.value(t, tz_hours=tz) <= 1.0 + 1e-9


def test_region_scoped_overlay_stays_in_region(make_scenario):
    sc = make_scenario(
        traffic={"base_concurrency": 1000, "regions": {"us": 0.5, "eu": 0.5}},
        verticals={"ecom": {}},
        events=[{"type": "flash_sale", "at": "+0s", "rise": 0, "hold": "1h", "regions": ["eu"]}],
    )
    pl = TrafficPlanner(sc, start_sim=0)
    eff = pl.effects(600)
    assert eff[("ecom", "web", "eu")].get("bias")
    assert not eff[("ecom", "web", "us")].get("bias")


def test_effects_prefer_most_recent_featured():
    a = Overlay({"type": "flash_sale", "at": 0, "rise": 0, "hold": 1000, "featured": {"sale_name": "old"}}, 0)
    b = Overlay({"type": "flash_sale", "at": 100, "rise": 0, "hold": 1000, "featured": {"sale_name": "new"}}, 0)
    feats = effects_for([a, b], "ecom", "web", 200)["featured"]
    assert feats[0]["sale_name"] == "new"


def test_peak_estimate_sees_short_overlays(make_scenario):
    sc = make_scenario(
        duration="6h",
        traffic={"base_concurrency": 1000, "regions": {"us": 1}},
        verticals={"ecom": {}},
        events=[{"type": "flash_sale", "at": "+2h7m", "rise": "30s", "hold": "2m", "decay": "30s", "magnitude": 5}],
    )
    peak, _ = TrafficPlanner(sc, start_sim=0).peak_estimate()
    assert peak > 4000


def test_journey_rejects_unreachable_states():
    j = Journey(
        start="a",
        states={
            "a": State(name="a", next={"exit": 1}),
            "orphan": State(name="orphan", next={"a": 1}),
        },
    )
    with pytest.raises(ValueError, match="unreachable"):
        j.validate({}, set())
    for v in ("streaming", "ecom", "gaming"):
        cat = Catalog.load(v)
        assert cat.app.get("name"), v  # catalog metadata survives parsing


def test_scheduler_discard_and_reschedule():
    sch = Scheduler()
    a, b = SimpleNamespace(sid=1), SimpleNamespace(sid=2)
    sch.push(10, a)
    sch.push(5, b)
    sch.push(20, a)  # reschedule supersedes the earlier entry
    assert len(sch) == 2 and sch.peek_time() == 5
    assert sch.pop_due(30) == (5, b)
    assert sch.pop_due(30) == (20, a)
    assert sch.pop_due(30) is None
    sch.push(1, a)
    sch.discard(a)
    assert sch.pop_due(30) is None and len(sch) == 0


def test_user_pool_never_double_books_and_reuses_overflow():
    pool = UserPool(1, ("ecom", "web", "us"), size=4, worker_id=0, n_workers=1)
    rng = random.Random(0)
    users = [pool.acquire(rng) for _ in range(pool.slots + 3)]
    idx = [u.index for u in users]
    assert len(set(idx)) == len(idx)
    for u in users[-3:]:
        pool.release(u)
    again = [pool.acquire(rng) for _ in range(3)]
    assert {u.index for u in again} == set(idx[-3:])  # overflow ids are recycled, not minted forever


def test_signup_and_installs_persist_across_sessions():
    pool = UserPool(1, ("gaming", "pc", "us"), size=4, worker_id=0, n_workers=1)
    rng = random.Random(0)
    u = pool.acquire(rng)
    u.extra["signed_up_at"] = START
    u.extra["installed"] = ["nebula_arena"]
    pool.release(u)
    while True:
        v = pool.acquire(rng)
        if v.index == u.index:
            break
        pool.release(v)
    assert v.registered and v.traits["createdAt"].startswith("2026-01-01")
    assert v.extra["installed"] == ["nebula_arena"]


def test_traits_use_segment_reserved_names():
    t = make_user(1, "ecom", "web", "us", 3).traits
    assert {"firstName", "lastName", "email", "createdAt"} <= set(t)
    assert "first_name" not in t and t["createdAt"].endswith("Z")


def test_device_contexts_are_consistent():
    app = {"name": "A", "version": "1", "build": 1}
    for i in range(300):
        u = make_user(1, "ecom", "ios", "us", i)
        ios = build_context("ios", u, app, i)
        if ios["device"]["model"].startswith("iPhone17"):
            assert ios["os"]["version"].startswith("18")
        if ios["device"]["model"].startswith("iPad"):
            assert "iPad;" in ios["userAgent"] and ios["os"]["name"] == "iPadOS"
        android = build_context("android", u, app, i)
        if android["device"]["name"] in ("Pixel 9 Pro", "Galaxy S24 Ultra"):
            assert int(android["os"]["version"]) >= 14
        ps5 = build_context("ps5", u, app, i)
        assert (ps5["screen"]["width"], ps5["screen"]["height"]) in {(3840, 2160), (2560, 1440), (1920, 1080)}


def test_clock_defaults_to_monotonic_time():
    c = SimClock(START, speed=10)
    assert abs(c.anchor_wall - time.monotonic()) < 5


# ---------------------------------------------------------------- SDK accounting


def test_oversize_messages_are_counted_and_drain_finishes(make_scenario):
    sc = make_scenario(sdk={"upload_size": 10, "upload_interval": 0.1, "max_retries": 0, "timeout": 1})
    cl = SimClient("k", "http://127.0.0.1:9", sc.sdk, send=True)
    try:
        cl.track(user_id="u", event="big", properties={"blob": "x" * 40_000})
        assert cl.drain(10)  # upstream would hang here: oversize items were never task_done()
        assert cl.counter.oversize == 1 and cl.undelivered == 0
    finally:
        for c in cl.consumers:
            c.pause()


def test_funnel_rate_is_zero_without_first_step():
    agg = StatsAggregator(1, {"ecom": {"purchase": ["product_viewed", "order_completed"]}})
    rep = agg.funnel_report()["ecom.purchase"]
    assert [s["rate"] for s in rep] == [0.0, 0.0]


# ---------------------------------------------------------------- CLI


def test_cli_validate_and_list(capsys):
    from trafficsim.cli import main

    assert main(["validate", "baseline_ecom"]) == 0
    assert "baseline_ecom" in capsys.readouterr().out
    assert main(["list"]) == 0
    assert "stress_100k" in capsys.readouterr().out


def test_cli_version(capsys):
    from trafficsim import __version__
    from trafficsim.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"trafficsim {__version__}"


def test_cli_dry_run_writes_exports(tmp_path, capsys):
    from trafficsim.cli import main

    rc = main(
        [
            "run",
            "baseline_gaming",
            "--dry-run",
            "--workers",
            "1",
            "--concurrency",
            "100",
            "--max-wall",
            "3",
            "--start-at",
            "-24h",
            "--out",
            str(tmp_path),
        ]
    )
    assert rc == 0
    run_dirs = list(tmp_path.iterdir())
    assert len(run_dirs) == 1
    files = {p.name for p in run_dirs[0].iterdir()}
    assert {"summary.json", "timeseries.csv", "timeseries.jsonl"} <= files
    summary = json.loads((run_dirs[0] / "summary.json").read_text())
    assert summary["total_events"] > 0 and summary["warnings"] == []


def test_bundled_scenarios_load_by_id():
    for sid in ("baseline_streaming", "ecom_flash_sale", "gaming_launch_day", "mixed_all", "stress_100k"):
        assert load_scenario(sid).name
