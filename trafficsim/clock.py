"""Simulated clock: maps wall time to simulated time with a speed multiplier.

The coordinator owns the authoritative clock and broadcasts its state; workers
rebuild an identical clock from that state so every process agrees on "now".

Wall time is `time.monotonic()`, which is system-wide on Linux, macOS and Windows,
so processes on one host share it and NTP or manual clock changes cannot make the
simulated clock jump. Event timestamps come from `now()` (sim epoch seconds).
"""

from __future__ import annotations

import time


class SimClock:
    def __init__(self, start_sim: float, speed: float = 1.0, paused: bool = False, wall_fn=time.monotonic):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self._wall = wall_fn
        self.anchor_wall = self._wall()
        self.anchor_sim = float(start_sim)
        self.speed = float(speed)
        self.paused = paused

    def now(self) -> float:
        if self.paused:
            return self.anchor_sim
        return self.anchor_sim + (self._wall() - self.anchor_wall) * self.speed

    def _rebase(self) -> None:
        self.anchor_sim = self.now()
        self.anchor_wall = self._wall()

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self._rebase()
        self.speed = float(speed)

    def pause(self) -> None:
        if not self.paused:
            self._rebase()
            self.paused = True

    def resume(self) -> None:
        if self.paused:
            self.anchor_wall = self._wall()
            self.paused = False

    def wall_until(self, sim_t: float) -> float:
        """Wall seconds until sim time `sim_t` is reached (inf while paused)."""
        if self.paused:
            return float("inf")
        return max(0.0, (sim_t - self.now()) / self.speed)

    def state(self) -> dict:
        return {
            "anchor_wall": self.anchor_wall,
            "anchor_sim": self.anchor_sim,
            "speed": self.speed,
            "paused": self.paused,
        }

    def load_state(self, st: dict) -> None:
        self.anchor_wall = st["anchor_wall"]
        self.anchor_sim = st["anchor_sim"]
        self.speed = st["speed"]
        self.paused = st["paused"]

    @classmethod
    def from_state(cls, st: dict, wall_fn=time.monotonic) -> SimClock:
        c = cls(st["anchor_sim"], st["speed"], st["paused"], wall_fn=wall_fn)
        c.load_state(st)
        return c
