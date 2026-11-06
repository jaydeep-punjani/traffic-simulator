"""Composable traffic patterns. Each returns a multiplier; the planner multiplies them.

Patterns may be scoped to verticals / platforms / regions. Times in pattern
definitions ("at", "start") are offsets from the scenario start ("+2h") or
absolute ISO timestamps.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .timeutil import parse_duration

DIURNAL_PROFILES: dict[str, list[float]] = {
    # local hour 0..23, peak normalised to 1.0
    "streaming": [0.42, 0.28, 0.18, 0.12, 0.09, 0.09, 0.13, 0.20, 0.26, 0.29, 0.31, 0.34,
                  0.39, 0.41, 0.41, 0.44, 0.51, 0.61, 0.74, 0.87, 0.97, 1.00, 0.90, 0.65],
    "ecom": [0.25, 0.15, 0.10, 0.07, 0.06, 0.08, 0.15, 0.30, 0.48, 0.60, 0.68, 0.78,
             0.88, 0.85, 0.75, 0.72, 0.74, 0.78, 0.85, 0.95, 1.00, 0.92, 0.70, 0.45],
    "gaming": [0.62, 0.45, 0.30, 0.20, 0.14, 0.11, 0.12, 0.16, 0.22, 0.27, 0.31, 0.36,
               0.42, 0.46, 0.50, 0.57, 0.66, 0.76, 0.86, 0.94, 0.99, 1.00, 0.95, 0.80],
}
DIURNAL_PROFILES["default"] = DIURNAL_PROFILES["streaming"]
WEEKLY_DEFAULT = [0.90, 0.88, 0.90, 0.95, 1.05, 1.20, 1.15]  # Mon..Sun


def resolve_time(value: Any, start: float) -> float:
    """Offset ('+2h', 3600) from scenario start, or absolute ISO timestamp."""
    if isinstance(value, (int, float)):
        return start + float(value)
    s = str(value).strip()
    if s.lower() in ("start", "now", ""):
        return start
    if s[0] in "+-" or s[0].isdigit() and not ("T" in s or "-" in s[1:]):
        return start + parse_duration(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def smoothstep(x: float) -> float:
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return x * x * (3 - 2 * x)


def envelope(t: float, at: float, rise: float, hold: float, decay: float) -> float:
    """0..1 shape: smooth rise, flat hold, smooth decay."""
    if t < at:
        return 0.0
    dt = t - at
    if dt < rise:
        return smoothstep(dt / rise) if rise > 0 else 1.0
    dt -= rise
    if dt < hold:
        return 1.0
    dt -= hold
    if dt < decay:
        return 1.0 - smoothstep(dt / decay)
    return 0.0


def _interp(points: list[tuple[float, float]], t: float) -> float:
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][1]


class Pattern:
    kind = "base"

    def __init__(self, spec: dict, start: float):
        self.spec = spec
        self.start = start
        self.verticals = set(spec.get("verticals") or ([spec["vertical"]] if spec.get("vertical") else []))
        self.platforms = set(spec.get("platforms") or [])
        self.regions = set(spec.get("regions") or [])

    def applies(self, vertical: str, platform: str, region: str) -> bool:
        return ((not self.verticals or vertical in self.verticals)
                and (not self.platforms or platform in self.platforms)
                and (not self.regions or region in self.regions))

    def value(self, t: float, tz_hours: float = 0.0, vertical: str = "") -> float:
        raise NotImplementedError


class Constant(Pattern):
    kind = "constant"

    def value(self, t, tz_hours=0.0, vertical=""):
        return float(self.spec.get("value", 1.0))


class Diurnal(Pattern):
    kind = "diurnal"

    def __init__(self, spec, start):
        super().__init__(spec, start)
        curve = spec.get("curve")
        if curve is not None and len(curve) != 24:
            raise ValueError("diurnal.curve must have 24 hourly values")
        self.curve = curve
        self.profile = spec.get("profile")
        self.floor = float(spec.get("floor", 0.0))
        self.strength = float(spec.get("strength", 1.0))

    def value(self, t, tz_hours=0.0, vertical=""):
        curve = self.curve or DIURNAL_PROFILES.get(self.profile or vertical, DIURNAL_PROFILES["default"])
        h = ((t / 3600.0) + tz_hours) % 24.0
        i = int(h)
        frac = h - i
        v = curve[i] + (curve[(i + 1) % 24] - curve[i]) * frac
        v = 1.0 - self.strength * (1.0 - v)
        return max(self.floor, v)


class Weekly(Pattern):
    kind = "weekly"

    def __init__(self, spec, start):
        super().__init__(spec, start)
        self.factors = spec.get("factors", WEEKLY_DEFAULT)
        if len(self.factors) != 7:
            raise ValueError("weekly.factors must have 7 values (Mon..Sun)")

    def value(self, t, tz_hours=0.0, vertical=""):
        local = t + tz_hours * 3600.0
        # 1970-01-01 was a Thursday (index 3 with Monday=0)
        day = int(local // 86400 + 3) % 7
        return float(self.factors[day])


class Ramp(Pattern):
    kind = "ramp"

    def __init__(self, spec, start):
        super().__init__(spec, start)
        self.t0 = resolve_time(spec.get("start", spec.get("at", "+0s")), start)
        self.dur = parse_duration(spec.get("duration", "1h"))
        self.v0 = float(spec.get("from", 0.0))
        self.v1 = float(spec.get("to", 1.0))

    def value(self, t, tz_hours=0.0, vertical=""):
        if t <= self.t0:
            return self.v0
        if self.dur <= 0 or t >= self.t0 + self.dur:
            return self.v1
        return self.v0 + (self.v1 - self.v0) * (t - self.t0) / self.dur


class Step(Pattern):
    kind = "step"

    def __init__(self, spec, start):
        super().__init__(spec, start)
        self.initial = float(spec.get("initial", 1.0))
        self.steps = sorted((resolve_time(s["at"], start), float(s["value"])) for s in spec.get("steps", []))

    def value(self, t, tz_hours=0.0, vertical=""):
        v = self.initial
        for at, val in self.steps:
            if t >= at:
                v = val
            else:
                break
        return v


class Spike(Pattern):
    kind = "spike"

    def __init__(self, spec, start):
        super().__init__(spec, start)
        self.at = resolve_time(spec.get("at", "+0s"), start)
        self.rise = parse_duration(spec.get("rise", "10m"))
        self.hold = parse_duration(spec.get("hold", "30m"))
        self.decay = parse_duration(spec.get("decay", "30m"))
        self.magnitude = float(spec.get("magnitude", 2.0))

    def value(self, t, tz_hours=0.0, vertical=""):
        return 1.0 + (self.magnitude - 1.0) * envelope(t, self.at, self.rise, self.hold, self.decay)


class Sine(Pattern):
    kind = "sine"

    def __init__(self, spec, start):
        super().__init__(spec, start)
        self.period = parse_duration(spec.get("period", "1h"))
        self.amplitude = float(spec.get("amplitude", 0.2))
        self.phase = parse_duration(spec.get("phase", 0))
        if self.period <= 0:
            raise ValueError("sine.period must be > 0")

    def value(self, t, tz_hours=0.0, vertical=""):
        return max(0.0, 1.0 + self.amplitude * math.sin(2 * math.pi * (t - self.start + self.phase) / self.period))


class Custom(Pattern):
    kind = "custom"

    def __init__(self, spec, start):
        super().__init__(spec, start)
        pts = spec.get("points") or []
        if not pts:
            raise ValueError("custom.points requires at least one [offset, value] pair")
        self.repeat = parse_duration(spec["repeat"]) if spec.get("repeat") else None
        self.points = sorted((parse_duration(p[0]), float(p[1])) for p in pts)

    def value(self, t, tz_hours=0.0, vertical=""):
        x = t - self.start
        if self.repeat:
            x %= self.repeat
        return _interp(self.points, x)


PATTERNS = {c.kind: c for c in (Constant, Diurnal, Weekly, Ramp, Step, Spike, Sine, Custom)}


def build_pattern(spec: dict, start: float) -> Pattern:
    kind = spec.get("type")
    if kind not in PATTERNS:
        raise ValueError(f"unknown pattern type {kind!r}; choose from {sorted(PATTERNS)}")
    return PATTERNS[kind](spec, start)
