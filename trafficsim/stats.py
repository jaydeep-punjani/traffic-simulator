"""Stats: per-worker counters (deltas) and the coordinator-side aggregator."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any


class WorkerStats:
    """Cheap counters updated on the hot path; `snapshot()` returns and resets deltas."""

    def __init__(self):
        self._reset()

    def _reset(self):
        self.by_type: dict[tuple, int] = {}  # (vertical, platform, type) -> n
        self.by_name: dict[tuple, int] = {}  # (vertical, event name) -> n
        self.by_key: dict[tuple, int] = {}  # (vertical, logical key) -> n  (funnels)
        self.errors = 0
        self.error_samples: list[str] = []
        self.started: dict[tuple, int] = {}  # (vertical, platform) -> n
        self.ended: dict[tuple, int] = {}
        self.events = 0

    def event(self, vertical, platform, kind, name, key, ok) -> None:
        self.events += 1
        kt = (vertical, platform, kind)
        self.by_type[kt] = self.by_type.get(kt, 0) + 1
        kn = (vertical, name)
        self.by_name[kn] = self.by_name.get(kn, 0) + 1
        if key:
            kk = (vertical, key)
            self.by_key[kk] = self.by_key.get(kk, 0) + 1

    def journey_error(self, tb: str) -> None:
        self.errors += 1
        if len(self.error_samples) < 3:
            self.error_samples.append(tb[-2000:])

    def session_started(self, vertical, platform):
        k = (vertical, platform)
        self.started[k] = self.started.get(k, 0) + 1

    def session_ended(self, vertical, platform):
        k = (vertical, platform)
        self.ended[k] = self.ended.get(k, 0) + 1

    def snapshot(self) -> dict:
        snap = {
            "events": self.events,
            "by_type": self.by_type,
            "by_name": self.by_name,
            "by_key": self.by_key,
            "started": self.started,
            "ended": self.ended,
            "errors": self.errors,
            "error_samples": self.error_samples,
        }
        self._reset()
        return snap


SDK_KEYS = ("sent", "failed", "dropped", "oversize", "undelivered", "queue_depth", "batches", "enqueued")


def _add(dst: dict, src: dict) -> None:
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


class StatsAggregator:
    """Merges worker snapshots into cumulative totals and a 1 Hz time series."""

    def __init__(self, n_workers: int, funnels: dict[str, dict[str, list[str]]] | None = None, history: int = 7200):
        self.n = n_workers
        self.lock = threading.RLock()
        self.funnels = funnels or {}
        self.started_wall = time.time()
        self.total_events = 0
        self.by_type: dict[tuple, int] = {}
        self.by_name: dict[tuple, int] = {}
        self.by_key: dict[tuple, int] = {}
        self.sessions_started = 0
        self.sessions_ended = 0
        self.workers: dict[int, dict] = {}  # latest gauges per worker
        self.sdk_prev: dict[int, dict] = {}
        self.series: deque = deque(maxlen=history)
        self._pending: dict[str, Any] = {"events": 0, "by_type": {}, "by_name": {}, "started": 0, "ended": 0}
        self._last_tick = time.time()
        self.peak_active = 0
        self.peak_eps = 0.0
        self.max_lag = 0.0
        self.journey_errors = 0
        self.error_samples: list[str] = []

    def ingest(self, wid: int, snap: dict) -> None:
        with self.lock:
            self.total_events += snap["events"]
            _add(self.by_type, snap["by_type"])
            _add(self.by_name, snap["by_name"])
            _add(self.by_key, snap["by_key"])
            st = sum(snap["started"].values())
            en = sum(snap["ended"].values())
            self.sessions_started += st
            self.sessions_ended += en
            self.journey_errors += snap.get("errors", 0)
            for tb in snap.get("error_samples", ()):
                if len(self.error_samples) < 5:
                    self.error_samples.append(tb)
            p = self._pending
            p["events"] += snap["events"]
            _add(p["by_type"], snap["by_type"])
            _add(p["by_name"], snap["by_name"])
            p["started"] += st
            p["ended"] += en
            self.workers[wid] = {
                "active": snap["active"],
                "sdk": snap["sdk"],
                "lag": snap["lag"],
                "sim_now": snap["sim_now"],
                "heap": snap.get("heap", 0),
                "queue_fill": snap.get("queue_fill", 0.0),
                "trimmed": snap.get("trimmed", 0),
                "wall": snap["wall"],
                "done": snap.get("done", False),
            }

    def tick(self, sim_now: float, targets: dict | None, speed: float, paused: bool) -> dict:
        """Close the current 1-second window and append a time-series point."""
        with self.lock:
            now = time.time()
            dt = max(1e-3, now - self._last_tick)
            self._last_tick = now
            p = self._pending
            self._pending = {"events": 0, "by_type": {}, "by_name": {}, "started": 0, "ended": 0}

            active_vp: dict[str, int] = {}
            sdk = self._sdk_totals()
            lag = 0.0
            for w in self.workers.values():
                for (v, pl), n in w["active"].items():
                    k = f"{v}/{pl}"
                    active_vp[k] = active_vp.get(k, 0) + n
                lag = max(lag, w["lag"])
            prev = self.sdk_prev.get(-1, {"sent": 0, "failed": 0, "dropped": 0})
            self.sdk_prev[-1] = sdk
            active = sum(active_vp.values())
            eps = p["events"] / dt
            self.peak_active = max(self.peak_active, active)
            self.peak_eps = max(self.peak_eps, eps)
            self.max_lag = max(self.max_lag, lag)
            eps_type: dict[str, float] = {}
            eps_vertical: dict[str, float] = {}
            eps_platform: dict[str, float] = {}
            for (v, pl, kind), n in p["by_type"].items():
                eps_type[kind] = eps_type.get(kind, 0) + n / dt
                eps_vertical[v] = eps_vertical.get(v, 0) + n / dt
                eps_platform[pl] = eps_platform.get(pl, 0) + n / dt
            target_total = 0.0
            target_vp: dict[str, float] = {}
            if targets:
                for (v, pl, _r), val in targets.items():
                    target_total += val
                    k = f"{v}/{pl}"
                    target_vp[k] = target_vp.get(k, 0) + val
            point = {
                "wall": now,
                "elapsed": now - self.started_wall,
                "sim_time": sim_now,
                "speed": speed,
                "paused": paused,
                "active": active,
                "active_by": active_vp,
                "target": round(target_total, 1),
                "target_by": {k: round(v, 1) for k, v in target_vp.items()},
                "eps": round(eps, 1),
                "eps_by_type": {k: round(v, 1) for k, v in eps_type.items()},
                "eps_by_vertical": {k: round(v, 1) for k, v in eps_vertical.items()},
                "eps_by_platform": {k: round(v, 1) for k, v in eps_platform.items()},
                "top_events": sorted(((f"{v}: {n}", round(c / dt, 1)) for (v, n), c in p["by_name"].items()), key=lambda x: -x[1])[:12],
                "sessions_started": p["started"],
                "sessions_ended": p["ended"],
                "sdk_sent_rate": round(max(0, sdk["sent"] - prev["sent"]) / dt, 1),
                "sdk_failed_rate": round(max(0, sdk["failed"] - prev["failed"]) / dt, 1),
                "sdk_dropped_rate": round(max(0, sdk["dropped"] - prev["dropped"]) / dt, 1),
                "sdk": sdk,
                "lag": round(lag, 2),
                "total_events": self.total_events,
            }
            self.series.append(point)
            return point

    def _sdk_totals(self) -> dict:
        sdk = dict.fromkeys(SDK_KEYS, 0)
        for w in self.workers.values():
            for k in SDK_KEYS:
                sdk[k] += w["sdk"].get(k, 0)
        return sdk

    def series_snapshot(self, last: int | None = None) -> list[dict]:
        with self.lock:
            pts = list(self.series)
        return pts[-last:] if last else pts

    def reset_tick(self) -> None:
        with self.lock:
            self._last_tick = time.time()

    def funnel_report(self) -> dict:
        """Step counts and conversion relative to the first step (0 when the first step never happened)."""
        with self.lock:
            out = {}
            for vertical, funnels in self.funnels.items():
                for fname, steps in funnels.items():
                    counts = [self.by_key.get((vertical, s), 0) for s in steps]
                    first = counts[0]
                    out[f"{vertical}.{fname}"] = [
                        {"step": s, "count": c, "rate": round(c / first, 4) if first else 0.0} for s, c in zip(steps, counts, strict=True)
                    ]
            return out

    def summary(self) -> dict:
        with self.lock:
            sdk = self._sdk_totals()
            elapsed = max(1e-3, time.time() - self.started_wall)
            return {
                "total_events": self.total_events,
                "avg_eps": round(self.total_events / elapsed, 1),
                "peak_eps": round(self.peak_eps, 1),
                "peak_active_sessions": self.peak_active,
                "sessions_started": self.sessions_started,
                "sessions_ended": self.sessions_ended,
                "max_lag_sim_seconds": round(self.max_lag, 2),
                "sessions_trimmed": sum(w.get("trimmed", 0) for w in self.workers.values()),
                "journey_errors": self.journey_errors,
                "journey_error_samples": list(self.error_samples),
                "sdk": sdk,
                "by_type": _group(self.by_type, lambda k: k[2]),
                "by_vertical": _group(self.by_type, lambda k: k[0]),
                "by_platform": _group(self.by_type, lambda k: f"{k[0]}/{k[1]}"),
                "by_event": {f"{v}: {n}": c for (v, n), c in sorted(self.by_name.items(), key=lambda kv: -kv[1])},
                "funnels": self.funnel_report(),
            }


def _group(d: dict, keyfn) -> dict:
    out: dict = {}
    for k, v in d.items():
        g = keyfn(k)
        out[g] = out.get(g, 0) + v
    return out
