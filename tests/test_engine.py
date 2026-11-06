import json
import queue
import time

import pytest

from trafficsim.clock import SimClock
from trafficsim.config import VERTICAL_PLATFORMS, Destinations
from trafficsim.contexts import LIBRARIES
from trafficsim.emitter import RecordingEmitter
from trafficsim.export import write_exports
from trafficsim.identity import make_user
from trafficsim.journeys import Catalog
from trafficsim.stats import StatsAggregator
from trafficsim.worker import Worker

START = 1_767_225_600.0  # 2026-01-01T00:00:00Z


def run_worker(sc, seconds=3.0, emitter=None, dest=None, send=False, stats_q=None, overlays=None):
    clock = SimClock(START, sc.speed)
    w = Worker(0, 1, sc, START, clock.state(), dest or Destinations(), send=send, emitter=emitter, overlays=overlays)
    w.run(stats_q=stats_q, max_wall=seconds)
    return w


@pytest.fixture(scope="module")
def recorded():
    from tests.conftest import scenario

    sc = scenario(traffic={"base_concurrency": 900, "regions": {"us": 0.6, "eu": 0.4}})
    em = RecordingEmitter()
    stats_q = queue.Queue()
    w = run_worker(sc, 4.0, emitter=em, stats_q=stats_q)
    return sc, w, em.messages, stats_q


def test_every_platform_emits_with_right_context(recorded):
    _sc, _w, msgs, _ = recorded
    seen = {}
    for m in msgs:
        lib = m["context"]["library"]["name"]
        seen.setdefault(lib, set()).add(m["type"])
    for platform, lib in LIBRARIES.items():
        assert lib["name"] in seen, platform
    web = [m for m in msgs if m["context"]["library"]["name"] == "analytics.js"]
    apps = [m for m in msgs if m["context"]["library"]["name"] in ("analytics-swift", "analytics-kotlin")]
    console = [m for m in msgs if m["context"]["library"]["name"] == "Analytics-CSharp"]
    assert web and all(m["type"] != "screen" for m in web)
    assert any(m["type"] == "page" for m in web) and all("userAgent" in m["context"] for m in web)
    assert all(m["type"] != "page" for m in apps + console)
    assert any(m["context"].get("os", {}).get("name") == "iOS" for m in apps)
    assert any(m["context"].get("os", {}).get("name") == "Android" for m in apps)
    assert any("PlayStation" in json.dumps(m["context"]) for m in console)
    assert any("Windows" in json.dumps(m["context"]) for m in console)
    assert all("app" in m["context"] and "device" in m["context"] for m in apps + console)


def test_event_names_come_from_catalogs(recorded):
    sc, _, msgs, _ = recorded
    names = set()
    for v in VERTICAL_PLATFORMS:
        names |= set(Catalog.load(v, sc.verticals[v]).events.values())
    tracked = {m["event"] for m in msgs if m["type"] == "track"}
    assert tracked and tracked <= names
    for expected in ("Video Playback Started", "Product Viewed", "Match Started"):
        assert expected in tracked


def test_video_spec_id_properties(recorded):
    _, _, msgs, _ = recorded
    video = [m for m in msgs if m["type"] == "track" and m["event"].startswith("Video ")]
    started = [m["properties"] for m in video if m["event"] == "Video Playback Started"]
    assert started
    for p in started:
        assert isinstance(p["content_asset_ids"], list) and isinstance(p["content_pod_ids"], list)
        assert "content_asset_id" not in p and "content_pod_id" not in p
    other_playback = [m["properties"] for m in video if m["event"].startswith("Video Playback ")]
    other_playback = [p for p in other_playback if "content_asset_ids" not in p]
    assert other_playback and all("content_asset_id" in p and "content_pod_id" in p for p in other_playback)
    content = [m["properties"] for m in video if m["event"].startswith("Video Content ")]
    assert content and all("asset_id" in p and "pod_id" in p for p in content)


def test_timestamps_follow_sim_clock(recorded):
    _sc, w, msgs, _ = recorded
    from datetime import datetime

    ts = [datetime.fromisoformat(m["timestamp"]).timestamp() for m in msgs]
    assert min(ts) >= START - 1
    assert max(ts) <= w.clock.now() + 1
    assert max(ts) - START > 600  # 4 wall seconds at 600x is ~40 sim minutes


def test_concurrency_tracks_target(recorded):
    _sc, w, _, _ = recorded
    active = sum(len(s) for s in w.cohort_sessions.values())
    assert 900 * 0.85 <= active <= 900 * 1.15
    # flat target: nothing should be trimmed (a double-counted departure allowance once let 600x runs reach ~1.5x)
    assert w.trimmed <= 9


def test_identity_flow(recorded):
    _, _, msgs, _ = recorded
    kinds = {m["type"] for m in msgs}
    assert {"track", "identify", "page", "screen"} <= kinds
    ident = [m for m in msgs if m["type"] == "identify"]
    assert all(m.get("userId") and m.get("traits") for m in ident)


def test_stats_aggregation_and_exports(recorded, tmp_path):
    sc, w, _msgs, stats_q = recorded
    funnels = {v: Catalog.load(v, sc.verticals[v]).funnels for v in VERTICAL_PLATFORMS}
    agg = StatsAggregator(1, funnels)
    snaps = []
    while not stats_q.empty():
        snaps.append(stats_q.get()[1])
    assert snaps
    for s in snaps:
        agg.ingest(0, s)
    point = agg.tick(w.clock.now(), w.planner.targets(w.clock.now()), sc.speed, False)
    assert point["active"] > 0 and point["target"] > 0 and set(point["active_by"]) >= {"ecom/web", "gaming/ps5"}
    summ = agg.summary()
    assert summ["total_events"] == sum(s["events"] for s in snaps)  # snapshots are deltas
    f = agg.funnel_report()
    assert f["ecom.purchase"][0]["step"] == "product_viewed" and f["ecom.purchase"][0]["count"] > 0
    assert all(s["rate"] <= 1.0 for steps in f.values() for s in steps)
    files = write_exports(tmp_path, summ, list(agg.series))
    assert (tmp_path / "summary.json").exists() and (tmp_path / "timeseries.csv").read_text().count("\n") >= 2
    assert files


def test_live_overlay_changes_target(make_scenario):
    sc = make_scenario(traffic={"base_concurrency": 200, "regions": {"us": 1}}, verticals={"ecom": {}})
    clock = SimClock(START, sc.speed)
    w = Worker(0, 1, sc, START, clock.state(), Destinations(), send=False, emitter=RecordingEmitter())
    before = w.planner.total_target(START + 600)
    w.handle(("overlay", {"type": "flash_sale", "at": 0, "rise": "1m", "hold": "1h", "magnitude": 3}))
    assert w.planner.total_target(START + 600) == pytest.approx(before * 3)
    w.handle(("stop", None))
    assert w.done


def test_events_per_sec_cap(make_scenario):
    sc = make_scenario(events_per_sec_cap=300, traffic={"base_concurrency": 2000, "regions": {"us": 1}})
    em = RecordingEmitter()
    t0 = time.monotonic()
    run_worker(sc, 2.0, emitter=em)
    assert len(em.messages) <= 300 * (time.monotonic() - t0) + 300


def test_user_pool_deterministic():
    a = make_user(1, "ecom", "ios", "us", 7)
    b = make_user(1, "ecom", "ios", "us", 7)
    c = make_user(2, "ecom", "ios", "us", 7)
    assert a.anonymous_id == b.anonymous_id and a.traits == b.traits
    assert a.anonymous_id != c.anonymous_id


def test_real_segment_sdk_delivers_to_collector(make_scenario, collector, dest):
    col, _ = collector
    col.reset()
    sc = make_scenario(
        traffic={"base_concurrency": 300, "regions": {"us": 1}}, sdk={"upload_size": 50, "upload_interval": 0.2, "threads": 2}
    )
    w = run_worker(sc, 2.5, dest=dest, send=True)
    drained = w.emitter.close(15)
    assert drained
    sdk = w.emitter.sdk_snapshot()
    assert sdk["enqueued"] > 100
    assert sdk["sent"] == sdk["enqueued"] and sdk["failed"] == 0 and sdk["dropped"] == 0
    st = col.stats()
    assert st["events"] == sdk["sent"]
    assert st["by_write_key"].get("ps5-key", 0) > 0 and st["by_write_key"].get("test-key", 0) > 0
    sample = col.samples[-1]["message"] if hasattr(col, "samples") and col.samples else None
    if sample:
        assert sample["messageId"] and sample["timestamp"] and "library" in sample["context"]


def test_sdk_failures_are_counted_not_silent(make_scenario):
    sc = make_scenario(
        traffic={"base_concurrency": 60, "regions": {"us": 1}},
        sdk={"upload_size": 20, "upload_interval": 0.1, "max_retries": 0, "timeout": 1},
    )
    w = run_worker(sc, 1.5, dest=Destinations(host="http://127.0.0.1:9"), send=True)
    w.emitter.close(10)
    sdk = w.emitter.sdk_snapshot()
    assert sdk["enqueued"] > 0 and sdk["failed"] > 0 and sdk["sent"] == 0
