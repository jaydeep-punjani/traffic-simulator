"""Coordinator: owns the authoritative clock and planner, spawns worker processes,
aggregates their stats into a 1 Hz time series and writes exports."""

from __future__ import annotations

import logging
import math
import multiprocessing as mp
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .clock import SimClock
from .config import ROOT, Destinations, Scenario
from .export import write_exports
from .journeys import Catalog
from .planner import TrafficPlanner
from .stats import StatsAggregator
from .timeutil import iso_ms, parse_duration, parse_start_at
from .worker import worker_main

log = logging.getLogger("trafficsim.runner")
RUNS_DIR = ROOT / "runs"


def default_workers(scenario: Scenario, peak: float) -> int:
    if scenario.workers:
        return scenario.workers
    cpus = os.cpu_count() or 2
    return max(1, min(cpus, math.ceil(peak / 1500)))


class Run:
    def __init__(self, scenario: Scenario, destinations: Destinations, send: bool = True, runs_dir: str | Path | None = None,
                 run_id: str | None = None, workers: int | None = None, drain_timeout: float = 60.0):
        self.scenario = scenario
        self.destinations = destinations
        self.send = send
        self.id = run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.out_dir = Path(runs_dir or RUNS_DIR) / self.id
        self.start_sim = parse_start_at(scenario.start_at)
        self.planner = TrafficPlanner(scenario, self.start_sim)
        self.peak, self.peak_t = self.planner.peak_estimate()
        self.n_workers = workers or default_workers(scenario, self.peak)
        self.clock = SimClock(self.start_sim, scenario.speed)
        funnels = {}
        for v in self.planner.verticals:
            cat = Catalog.load(v, scenario.verticals[v])
            funnels[v] = cat.funnels
        self.agg = StatsAggregator(self.n_workers, funnels)
        self.drain_timeout = drain_timeout
        self.state = "created"
        self.errors: list[str] = []
        self.listeners: list[Callable[[dict], None]] = []
        self.procs: list = []
        self.ctrl_qs: list = []
        self.finished_workers: set[int] = set()
        self.exports: dict | None = None
        self.started_wall: float | None = None
        self.ended_wall: float | None = None
        self.live_overlays: list[dict] = []
        self._lock = threading.Lock()
        self._done = threading.Event()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "Run":
        ctx = mp.get_context("spawn")
        self.stats_q = ctx.Queue()
        self.clock = SimClock(self.start_sim, self.scenario.speed)
        state = self.clock.state()
        data = self.scenario.model_dump()
        dest = self.destinations.model_dump()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for wid in range(self.n_workers):
            q = ctx.Queue()
            p = ctx.Process(target=worker_main, name=f"trafficsim-w{wid}", daemon=True,
                            args=(wid, self.n_workers, data, self.start_sim, state, dest, self.send, str(self.out_dir),
                                  [], q, self.stats_q, self.drain_timeout))
            p.start()
            self.procs.append(p)
            self.ctrl_qs.append(q)
        self.state = "running"
        self.started_wall = time.time()
        self.agg.started_wall = self.started_wall
        threading.Thread(target=self._collect, daemon=True, name="trafficsim-collect").start()
        threading.Thread(target=self._tick, daemon=True, name="trafficsim-tick").start()
        log.info("run %s started: %d workers, peak target %.0f sessions", self.id, self.n_workers, self.peak)
        return self

    def _broadcast(self, msg) -> None:
        for q in self.ctrl_qs:
            q.put(msg)

    def _collect(self) -> None:
        while len(self.finished_workers) < self.n_workers:
            try:
                wid, snap = self.stats_q.get(timeout=0.5)
            except queue.Empty:
                if all(not p.is_alive() for p in self.procs):
                    # workers died without saying goodbye
                    for i, p in enumerate(self.procs):
                        if i not in self.finished_workers:
                            self.errors.append(f"worker {i} exited with code {p.exitcode}")
                            self.finished_workers.add(i)
                continue
            if snap is None:
                self.finished_workers.add(wid)
            elif "error" in snap:
                self.errors.append(f"worker {wid}: {snap['error']}")
                log.error("worker %d failed:\n%s", wid, snap["error"])
            else:
                self.agg.ingest(wid, snap)
        self._finalize()

    def _tick(self) -> None:
        next_t = time.time()
        while not self._done.is_set():
            next_t += 1.0
            self._done.wait(max(0.0, next_t - time.time()))
            if self._done.is_set():
                break
            now = self.clock.now()
            end = self.planner.end
            if end is not None and now >= end:
                now = end
            point = self.agg.tick(now, self.planner.targets(now), self.clock.speed, self.clock.paused)
            point["state"] = self.state
            point["sim_time_iso"] = iso_ms(now)
            point["overlays"] = [{"name": ov.name, "type": ov.kind, "intensity": round(ov.intensity(now), 3)}
                                 for ov in self.planner.overlays if ov.intensity(now) > 0]
            out = dict(point, funnels=self.agg.funnel_report())
            for fn in list(self.listeners):
                try:
                    fn(out)
                except Exception:  # listener problems must not stop the run
                    log.exception("listener failed")
            if self.state == "running" and end is not None and now >= end:
                self.state = "stopping"

    def _finalize(self) -> None:
        with self._lock:
            if self.state == "finished":
                return
            self.state = "finishing"
            for p in self.procs:
                p.join(timeout=5)
            self.ended_wall = time.time()
            self.exports = write_exports(self.out_dir, self.summary(), list(self.agg.series))
            self.state = "finished"
        self._done.set()
        for fn in list(self.listeners):
            try:
                fn({"state": "finished", "summary": self.summary(), "exports": self.exports})
            except Exception:
                log.exception("listener failed")

    # ------------------------------------------------------------------ control
    def pause(self) -> None:
        self.clock.pause()
        self._broadcast(("clock", self.clock.state()))
        if self.state == "running":
            self.state = "paused"

    def resume(self) -> None:
        self.clock.resume()
        self._broadcast(("clock", self.clock.state()))
        if self.state == "paused":
            self.state = "running"

    def set_speed(self, speed: float) -> None:
        self.clock.set_speed(speed)
        self._broadcast(("clock", self.clock.state()))

    def trigger(self, spec: dict) -> dict:
        """Start an overlay event now (or at spec['at'] relative to now)."""
        spec = dict(spec)
        delay = spec.pop("delay", 0) or 0
        spec["at"] = self.clock.now() + parse_duration(delay) - self.planner.start  # offset from scenario start
        ov = self.planner.add_overlay(spec)
        self._broadcast(("overlay", spec))
        d = ov.describe()
        self.live_overlays.append(d)
        return d

    def stop(self) -> None:
        if self.state in ("finished", "finishing"):
            return
        self.state = "stopping"
        self._broadcast(("stop", None))

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    # ------------------------------------------------------------------ views
    def summary(self) -> dict:
        s = self.agg.summary()
        wall = (self.ended_wall or time.time()) - (self.started_wall or time.time())
        now = self.clock.now()
        if self.planner.end is not None:
            now = min(now, self.planner.end)
        s.update({
            "run_id": self.id, "scenario": self.scenario.name, "state": self.state, "workers": self.n_workers,
            "send": self.send, "destination": self.destinations.redacted(),
            "sim_start": iso_ms(self.start_sim), "sim_now": iso_ms(now), "sim_elapsed_s": round(now - self.start_sim, 1),
            "wall_elapsed_s": round(wall, 1), "speed": self.clock.speed,
            "planned_peak_sessions": round(self.peak), "planned_peak_at": iso_ms(self.peak_t),
            "avg_eps": round(s["total_events"] / max(wall, 1e-3), 1),
            "overlays": [ov.describe() for ov in self.planner.overlays], "errors": self.errors,
            "scenario_config": self.scenario.model_dump(),
        })
        return s

    def status(self) -> dict:
        last = self.agg.series[-1] if self.agg.series else None
        now = self.clock.now()
        return {"run_id": self.id, "state": self.state, "scenario": self.scenario.name, "workers": self.n_workers,
                "speed": self.clock.speed, "paused": self.clock.paused, "sim_time": now, "sim_time_iso": iso_ms(now),
                "sim_start": self.start_sim, "sim_end": self.planner.end, "planned_peak": round(self.peak),
                "last": last, "funnels": self.agg.funnel_report(), "errors": self.errors[-5:], "exports": self.exports,
                "overlays": [ov.describe() for ov in self.planner.overlays]}
