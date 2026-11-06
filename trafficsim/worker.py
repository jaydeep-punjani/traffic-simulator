"""Worker process: owns a shard of the simulated user population.

Each worker rebuilds the planner and clock from the coordinator's state, then
loops: spawn arrivals per cohort to track target concurrency, resume due
sessions from a min-heap (each resume emits events through the Segment SDK),
and report stats once per wall second.

Arrival control per cohort (worker share of the target `T`):
    lambda = departures_ewma + (T - active) / tau
The feed-forward term (observed natural departure rate) is Little's law
without having to know the realised session length; the proportional term
pulls concurrency to target. When active > 1.15 * T, excess sessions are told
to end now (they emit their normal exit events).
"""

from __future__ import annotations

import logging
import math
import queue
import random
import time
import traceback
from pathlib import Path

import numpy as np

from .clock import SimClock
from .config import DEFAULT_SESSION_MINUTES, Destinations, Scenario
from .contexts import build_context
from .emitter import Emitter
from .identity import UserPool
from .journeys import Catalog, Session
from .planner import TrafficPlanner
from .scheduler import Scheduler
from .stats import WorkerStats
from .verticals import RUNTIMES

log = logging.getLogger("trafficsim.worker")

SPAWN_INTERVAL = 0.25  # wall seconds between arrival/effects updates
STATS_INTERVAL = 1.0  # wall seconds between stats reports
SLICE = 0.05  # wall seconds of session processing between housekeeping
WARM_SPREAD = 60.0  # sim seconds over which warm-start sessions take their first step
DEPARTURE_TAU = 300.0  # sim seconds, EWMA time constant for natural departure rate
BACKPRESSURE_FILL = 0.8  # pause processing while any SDK queue is this full


class Worker:
    def __init__(
        self,
        wid: int,
        n_workers: int,
        scenario: Scenario,
        start_sim: float,
        clock_state: dict,
        destinations: Destinations,
        send: bool = True,
        out_dir: str | None = None,
        overlays: list | None = None,
        emitter=None,
    ):
        self.wid = wid
        self.n = n_workers
        self.scenario = scenario
        self.planner = TrafficPlanner(scenario, start_sim)
        for spec in overlays or []:
            self.planner.add_overlay(spec)
        self.clock = SimClock.from_state(clock_state)
        seed = scenario.seed * 1_000_003 + wid
        self.rng = random.Random(seed)
        self.nprng = np.random.default_rng(seed)
        self.stats = WorkerStats()
        sample_path = None
        if out_dir and scenario.sample_payloads > 0:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            sample_path = str(Path(out_dir) / f"payloads-w{wid}.jsonl")
        self.emitter = emitter or Emitter(
            destinations,
            scenario.sdk,
            self.planner.vp_pairs,
            self.stats,
            send=send,
            sample_rate=scenario.sample_payloads,
            sample_path=sample_path,
            rng=random.Random(seed + 7),
        )
        if emitter is not None:
            self.emitter.stats = self.stats

        self.runtimes = {}
        self.lengths = {}
        for v in self.planner.verticals:
            vc = scenario.verticals[v]
            self.runtimes[v] = RUNTIMES[v](Catalog.load(v, vc), self.emitter, self.rng, scenario.seed)
            sl = vc.session
            mean = (sl.mean_minutes or DEFAULT_SESSION_MINUTES[v]) * 60.0
            mu = math.log(mean) - sl.sigma**2 / 2
            self.lengths[v] = (mu, sl.sigma, sl.min_minutes * 60.0, sl.max_minutes * 60.0)

        peaks = self.planner.cohort_peaks()
        self.pools = {
            c: UserPool(scenario.seed, c, int(peaks[c] * scenario.verticals[c[0]].user_pool_factor) + 50, wid, n_workers)
            for c in self.planner.cohorts
        }
        self.cohort_sessions: dict[tuple, dict[int, Session]] = {c: {} for c in self.planner.cohorts}
        self.departures: dict[tuple, int] = dict.fromkeys(self.planner.cohorts, 0)
        self.dep_rate: dict[tuple, float] = {}
        self.sched = Scheduler()
        self.next_sid = wid
        self.last_spawn_sim: float | None = None
        self.tokens = 0.0
        self.cap = scenario.events_per_sec_cap / n_workers if scenario.events_per_sec_cap else None
        self.end = self.planner.end
        self.done = False
        self.lag = 0.0
        self.trimmed = 0

    # ------------------------------------------------------------------ sessions
    def _budget(self, v: str, warm: bool = False) -> float:
        mu, sigma, lo, hi = self.lengths[v]
        if not warm:
            return min(hi, max(lo, float(self.nprng.lognormal(mu, sigma))))
        # A session observed in progress is length-biased (inspection paradox): for
        # lognormal(mu, s) lengths that is lognormal(mu + s^2, s); its remaining
        # lifetime is uniform over that length.
        total = min(hi, max(lo, float(self.nprng.lognormal(mu + sigma**2, sigma))))
        return max(30.0, total * self.rng.random())

    def new_session(self, cohort: tuple, start: float, warm: bool = False) -> Session:
        v, p, _ = cohort
        vx = self.runtimes[v]
        user = self.pools[cohort].acquire(self.rng)
        ctx = build_context(p, user, vx.app, user.seed)
        budget = self._budget(v, warm)
        sid = self.next_sid
        self.next_sid += self.n
        s = Session(sid, cohort, user, ctx, start, budget, vx, warm=warm)
        self.cohort_sessions[cohort][sid] = s
        self.sched.push(start, s)
        self.stats.session_started(v, p)
        return s

    def end_session(self, s: Session, trimmed: bool = False) -> None:
        self.sched.discard(s)
        if self.cohort_sessions[s.cohort].pop(s.sid, None) is None:
            return
        self.pools[s.cohort].release(s.user)
        self.stats.session_ended(s.vertical, s.platform)
        if not (trimmed or s.ending):
            self.departures[s.cohort] += 1

    def step(self, t: float, s: Session) -> None:
        s.now = t
        try:
            wait = next(s.gen)
        except StopIteration:
            self.end_session(s)
            return
        except Exception:  # a journey bug must not take the worker down
            tb = traceback.format_exc()
            log.error("session %s crashed:\n%s", s.sid, tb)
            self.stats.journey_error(tb)
            self.end_session(s)
            return
        self.sched.push(t + max(0.05, float(wait)), s)

    # ------------------------------------------------------------------ control
    def mean_length(self, v: str) -> float:
        mu, sigma, lo, hi = self.lengths[v]
        return min(hi, max(lo, math.exp(mu + sigma**2 / 2)))

    def apply_effects(self, now: float) -> None:
        for (v, p, r), eff in self.planner.effects(now).items():
            self.runtimes[v].effects[(p, r)] = eff

    def warm_start(self, now: float) -> None:
        self.apply_effects(now)
        for c, tgt in self.planner.targets(now).items():
            k = int(tgt / self.n + self.rng.random())
            for _ in range(k):
                self.new_session(c, now + self.rng.random() * WARM_SPREAD, warm=True)
            self.dep_rate[c] = (tgt / self.n) / self.mean_length(c[0])
        self.last_spawn_sim = now

    def spawn(self, now: float) -> None:
        if self.last_spawn_sim is None:
            self.last_spawn_sim = now
            return
        dt = now - self.last_spawn_sim
        if dt <= 0:
            return
        last = self.last_spawn_sim
        self.last_spawn_sim = now
        tau = self.scenario.traffic.arrival_tau_seconds
        alpha = 1.0 - math.exp(-dt / DEPARTURE_TAU)
        for c, tgt in self.planner.targets(now).items():
            tw = tgt / self.n
            sessions = self.cohort_sessions[c]
            active = len(sessions)
            dep = self.departures[c] / dt
            self.departures[c] = 0
            prev = self.dep_rate.get(c)
            if prev is None:
                prev = tw / self.mean_length(c[0])
            rate = prev + alpha * (dep - prev)
            self.dep_rate[c] = rate
            lam = rate + (tw - active) / tau
            if lam > 0:
                k = int(self.nprng.poisson(min(lam * dt, 1e7)))
                # never overshoot target by >15%, allowing for sessions that depart within this window
                k = min(k, max(0, int(tw * 1.15 + 1 + rate * dt * 1.5) - active))
                for _ in range(k):
                    self.new_session(c, last + dt * self.rng.random())
            if active > tw * 1.15 + 1:
                excess = int(active - tw)
                for s in list(sessions.values()):
                    if excess <= 0:
                        break
                    if s.ending:
                        excess -= 1
                        continue
                    s.ending = True
                    self.sched.push(now, s)  # wakes now, sees `ending`, emits exit events
                    self.trimmed += 1
                    excess -= 1
        self.apply_effects(now)

    def handle(self, msg) -> None:
        kind, payload = msg
        if kind == "clock":
            self.clock.load_state(payload)
        elif kind == "overlay":
            self.planner.add_overlay(payload)
        elif kind == "stop":
            self.done = True

    def snapshot(self, now: float) -> dict:
        snap = self.stats.snapshot()
        active: dict[tuple, int] = {}
        if not self.done:
            for (v, p, _r), sessions in self.cohort_sessions.items():
                active[(v, p)] = active.get((v, p), 0) + len(sessions)
        snap.update(
            active=active,
            sdk=self.emitter.sdk_snapshot(),
            lag=self.lag,
            sim_now=now,
            wall=time.time(),
            heap=len(self.sched),
            queue_fill=self.emitter.max_queue_fill(),
            done=self.done,
            trimmed=self.trimmed,
        )
        return snap

    # ------------------------------------------------------------------ main loop
    def run(self, ctrl_q=None, stats_q=None, max_wall: float | None = None) -> None:
        now = self.clock.now()
        if self.scenario.warm_start:
            self.warm_start(now)
        else:
            self.apply_effects(now)
            self.last_spawn_sim = now
        wall0 = time.monotonic()
        next_spawn = next_stats = time.monotonic()
        last_cap = time.monotonic()
        while not self.done:
            if ctrl_q is not None:
                try:
                    while True:
                        self.handle(ctrl_q.get_nowait())
                except queue.Empty:
                    pass
                if self.done:
                    break
            mono = time.monotonic()
            if max_wall is not None and mono - wall0 > max_wall:
                break
            now = self.clock.now()
            if self.end is not None and now >= self.end:
                now = self.end
            paused = self.clock.paused
            if mono >= next_spawn:
                if not paused and (self.end is None or now < self.end):
                    self.spawn(now)
                next_spawn = mono + SPAWN_INTERVAL
            if self.cap is not None:
                self.tokens = min(self.cap, self.tokens + (mono - last_cap) * self.cap)
                last_cap = mono

            # resume due sessions for up to SLICE wall seconds
            slice_end = mono + SLICE
            n = 0
            throttled = False
            while True:
                if self.cap is not None and self.tokens <= 0:
                    throttled = True
                    break
                item = self.sched.pop_due(now)
                if item is None:
                    break
                before = self.stats.events
                self.step(*item)
                if self.cap is not None:
                    self.tokens -= self.stats.events - before
                n += 1
                if (n & 127) == 0:
                    if time.monotonic() > slice_end:
                        break
                    if self.emitter.max_queue_fill() > BACKPRESSURE_FILL:
                        throttled = True
                        break
            nxt = self.sched.peek_time()
            self.lag = max(0.0, now - nxt) if nxt is not None else 0.0

            mono = time.monotonic()
            if mono >= next_stats:
                if stats_q is not None:
                    stats_q.put((self.wid, self.snapshot(now)))
                next_stats = mono + STATS_INTERVAL
            if self.end is not None and now >= self.end and (nxt is None or nxt > self.end):
                break
            if paused:
                time.sleep(0.05)  # sim time is frozen: nothing becomes due until resume
            elif throttled:
                time.sleep(0.01)
            elif nxt is None or nxt > now:
                wait = self.clock.wall_until(nxt) if nxt is not None else SPAWN_INTERVAL
                time.sleep(max(0.0, min(wait, next_spawn - mono, 0.05)))
        self.done = True


def worker_main(
    wid: int,
    n_workers: int,
    scenario_data: dict,
    start_sim: float,
    clock_state: dict,
    dest_data: dict,
    send: bool,
    out_dir: str | None,
    overlays: list,
    ctrl_q,
    stats_q,
    drain_timeout: float = 60.0,
) -> None:
    logging.basicConfig(level=logging.WARNING, format=f"[w{wid}] %(levelname)s %(name)s: %(message)s")
    logging.getLogger("segment").setLevel(logging.CRITICAL)  # failures are counted and surfaced via stats
    logging.getLogger("backoff").setLevel(logging.CRITICAL)
    logging.getLogger("trafficsim.emitter").setLevel(logging.ERROR)
    w = None
    try:
        scenario = Scenario.model_validate(scenario_data)
        dest = Destinations.model_validate(dest_data)
        w = Worker(wid, n_workers, scenario, start_sim, clock_state, dest, send=send, out_dir=out_dir, overlays=overlays)
        w.run(ctrl_q, stats_q)
        w.emitter.close(drain_timeout)
        w.done = True
        stats_q.put((wid, w.snapshot(w.clock.now())))
    except Exception:
        stats_q.put((wid, {"error": traceback.format_exc()}))
    finally:
        stats_q.put((wid, None))
