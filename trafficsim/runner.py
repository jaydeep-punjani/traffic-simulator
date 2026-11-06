"""Coordinator: owns the authoritative clock and planner, spawns worker processes,
aggregates their stats into a 1 Hz time series and writes exports."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import multiprocessing as mp
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .clock import SimClock
from .config import RUNS_DIR, Destinations, Scenario
from .export import write_exports
from .journeys import Catalog
from .planner import TrafficPlanner
from .stats import StatsAggregator
from .timeutil import iso_ms, parse_duration, parse_start_at
from .worker import worker_main

log = logging.getLogger("trafficsim.runner")


STOP_GRACE = 15.0  # seconds beyond drain_timeout that workers get after stop/end before termination


def timestamp_warnings(scenario: Scenario, start_sim: float, end_sim: float | None, now: float | None = None) -> list[str]:
    """Explain when a run will stamp events in the future (collectors may reject or skew-correct them).

    Sim time T is reached at wall time now + (T - start_sim) / speed, so the largest
    lead over the wall clock is at the end of the run.
    """
    now = time.time() if now is None else now
    horizon = end_sim if end_sim is not None else start_sim + 7 * 86400
    ahead = horizon - (now + (horizon - start_sim) / scenario.speed)
    if ahead <= 60:
        return []
    return [
        f"event timestamps will run up to {ahead / 3600:.1f}h ahead of wall-clock time "
        f"(start_at={scenario.start_at!r}, speed {scenario.speed:g}x). Collectors that skew-correct with sentAt "
        "may shift them; set start_at in the past (e.g. -24h) to keep timestamps historical. "
        "See docs/operations.md#timestamps."
    ]


def default_workers(scenario: Scenario, peak: float) -> int:
    if scenario.workers:
        return scenario.workers
    cpus = os.cpu_count() or 2
    return max(1, min(cpus, math.ceil(peak / 1500)))


class Run:
    def __init__(
        self,
        scenario: Scenario,
        destinations: Destinations,
        send: bool = True,
        runs_dir: str | Path | None = None,
        run_id: str | None = None,
        workers: int | None = None,
        drain_timeout: float = 60.0,
    ):
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
        self._tick_stop = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._series_fh: TextIO | None = None
        self._stopping_since: float | None = None
        self.warnings = timestamp_warnings(scenario, self.start_sim, self.planner.end)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> Run:
        ctx = mp.get_context("spawn")
        self.stats_q = ctx.Queue()
        self.clock = SimClock(self.start_sim, self.scenario.speed)
        state = self.clock.state()
        data = self.scenario.model_dump()
        dest = self.destinations.model_dump()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._series_fh = open(self.out_dir / "timeseries.jsonl", "a", encoding="utf-8")  # noqa: SIM115 (closed in _finalize)
        for wid in range(self.n_workers):
            q = ctx.Queue()
            p = ctx.Process(
                target=worker_main,
                name=f"trafficsim-w{wid}",
                daemon=True,
                args=(
                    wid,
                    self.n_workers,
                    data,
                    self.start_sim,
                    state,
                    dest,
                    self.send,
                    str(self.out_dir),
                    [],
                    q,
                    self.stats_q,
                    self.drain_timeout,
                ),
            )
            p.start()
            self.procs.append(p)
            self.ctrl_qs.append(q)
        self.state = "running"
        self.started_wall = time.time()
        self.agg.started_wall = self.started_wall
        self.agg.reset_tick()
        self._tick_thread = threading.Thread(target=self._tick, daemon=True, name="trafficsim-tick")
        threading.Thread(target=self._collect, daemon=True, name="trafficsim-collect").start()
        self._tick_thread.start()
        log.info("run %s started: %d workers, peak target %.0f sessions", self.id, self.n_workers, self.peak)
        return self

    def _broadcast(self, msg) -> None:
        for q in self.ctrl_qs:
            with contextlib.suppress(ValueError, OSError):  # queue already closed during shutdown
                q.put(msg)

    def _mark_stopping(self) -> None:
        if self._stopping_since is None:
            self._stopping_since = time.monotonic()
        if self.state in ("running", "paused"):
            self.state = "stopping"

    def _reap_stragglers(self) -> None:
        """Workers get drain_timeout + STOP_GRACE after a stop/end to flush; then they are terminated."""
        if self._stopping_since is None:
            return
        if time.monotonic() - self._stopping_since < self.drain_timeout + STOP_GRACE:
            return
        for i, p in enumerate(self.procs):
            if i in self.finished_workers:
                continue
            if p.is_alive():
                self.errors.append(f"worker {i} did not finish within {self.drain_timeout + STOP_GRACE:.0f}s of stop; terminated")
                p.terminate()
                p.join(3)
                if p.is_alive():
                    p.kill()
            self.finished_workers.add(i)

    def _collect(self) -> None:
        try:
            while len(self.finished_workers) < self.n_workers:
                self._reap_stragglers()
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
        except Exception:
            log.exception("stats collector failed")
            self.errors.append("stats collector failed; see logs")
        finally:
            self._finalize()

    def _emit_point(self) -> None:
        now = self.clock.now()
        end = self.planner.end
        if end is not None and now >= end:
            now = end
        point = self.agg.tick(now, self.planner.targets(now), self.clock.speed, self.clock.paused)
        point["state"] = self.state
        point["sim_time_iso"] = iso_ms(now)
        point["overlays"] = [
            {"name": ov.name, "type": ov.kind, "intensity": round(ov.intensity(now), 3)}
            for ov in self.planner.overlays
            if ov.intensity(now) > 0
        ]
        if self._series_fh is not None:
            try:
                self._series_fh.write(json.dumps(point, default=str) + "\n")
                self._series_fh.flush()
            except OSError:
                log.exception("could not append to timeseries.jsonl")
        out = dict(point, funnels=self.agg.funnel_report())
        for fn in list(self.listeners):
            try:
                fn(out)
            except Exception:  # listener problems must not stop the run
                log.exception("listener failed")
        if end is not None and now >= end:
            self._mark_stopping()

    def _tick(self) -> None:
        next_t = time.monotonic()
        while not self._tick_stop.is_set():
            next_t += 1.0
            self._tick_stop.wait(max(0.0, next_t - time.monotonic()))
            if self._tick_stop.is_set():
                break
            try:
                self._emit_point()
            except Exception:
                log.exception("tick failed")

    def _finalize(self) -> None:
        with self._lock:
            if self.state == "finished":
                return
            self.state = "finishing"
            for p in self.procs:
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
            self.clock.pause()  # freeze sim time so the summary and status agree
            self._tick_stop.set()
            if self._tick_thread is not None:
                self._tick_thread.join(timeout=5)
            try:
                self._emit_point()  # final point includes the workers' closing snapshots
            except Exception:
                log.exception("final tick failed")
            if self._series_fh is not None:
                self._series_fh.close()
                self._series_fh = None
            self.ended_wall = time.time()
            self.state = "finished"
            try:
                self.exports = write_exports(self.out_dir, self.summary())
            except Exception as e:
                log.exception("writing exports failed")
                self.errors.append(f"writing exports failed: {e}")
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
        self._mark_stopping()
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
        s.update(
            {
                "run_id": self.id,
                "scenario": self.scenario.name,
                "state": self.state,
                "workers": self.n_workers,
                "send": self.send,
                "destination": self.destinations.redacted(),
                "sim_start": iso_ms(self.start_sim),
                "sim_now": iso_ms(now),
                "sim_elapsed_s": round(now - self.start_sim, 1),
                "wall_elapsed_s": round(wall, 1),
                "speed": self.clock.speed,
                "planned_peak_sessions": round(self.peak),
                "planned_peak_at": iso_ms(self.peak_t),
                "avg_eps": round(s["total_events"] / max(wall, 1e-3), 1),
                "overlays": [ov.describe() for ov in self.planner.overlays],
                "errors": self.errors,
                "warnings": self.warnings,
                "scenario_config": self.scenario.model_dump(),
            }
        )
        return s

    def status(self) -> dict:
        pts = self.agg.series_snapshot(1)
        last = pts[-1] if pts else None
        now = self.clock.now()
        return {
            "run_id": self.id,
            "state": self.state,
            "scenario": self.scenario.name,
            "workers": self.n_workers,
            "speed": self.clock.speed,
            "paused": self.clock.paused,
            "sim_time": now,
            "sim_time_iso": iso_ms(now),
            "sim_start": self.start_sim,
            "sim_end": self.planner.end,
            "planned_peak": round(self.peak),
            "last": last,
            "funnels": self.agg.funnel_report(),
            "errors": self.errors[-5:],
            "warnings": self.warnings,
            "exports": self.exports,
            "overlays": [ov.describe() for ov in self.planner.overlays],
        }
