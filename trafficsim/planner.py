"""TrafficPlanner: turns patterns x overlays into target concurrency per cohort.

A cohort is (vertical, platform, region). Base share of a cohort =
base_concurrency * vertical_share * platform_share * region_share.
"""

from __future__ import annotations

from .config import REGIONS, Scenario
from .events import Overlay, effects_for
from .patterns import build_pattern
from .timeutil import parse_duration, parse_start_at

Cohort = tuple[str, str, str]


class TrafficPlanner:
    def __init__(self, scenario: Scenario, start_sim: float | None = None):
        self.scenario = scenario
        self.start = parse_start_at(scenario.start_at) if start_sim is None else start_sim
        self.end = None if scenario.duration in (None, "", 0) else self.start + parse_duration(scenario.duration)
        tr = scenario.traffic
        self.patterns = [build_pattern(p, self.start) for p in tr.patterns]
        self.overlays: list[Overlay] = [Overlay(e.model_dump(), self.start) for e in scenario.events]

        verts = scenario.enabled_verticals()
        vtotal = sum(scenario.verticals[v].weight for v in verts)
        rtotal = sum(tr.regions.values())
        self.base: dict[Cohort, float] = {}
        for v in verts:
            vc = scenario.verticals[v]
            mix = vc.platforms or {}  # filled with the vertical default by Scenario validation
            ptotal = sum(mix.values())
            for p, pw in mix.items():
                for r, rw in tr.regions.items():
                    if rw <= 0:
                        continue
                    self.base[(v, p, r)] = tr.base_concurrency * (vc.weight / vtotal) * (pw / ptotal) * (rw / rtotal)
        self.cohorts: list[Cohort] = list(self.base)
        self.verticals = verts
        self.vp_pairs = sorted({(v, p) for v, p, _ in self.cohorts})

    def add_overlay(self, spec: dict, start_ref: float | None = None) -> Overlay:
        ov = Overlay(spec, self.start if start_ref is None else start_ref)
        self.overlays.append(ov)
        return ov

    def multiplier(self, cohort: Cohort, t: float) -> float:
        v, p, r = cohort
        tz = REGIONS[r]["tz"]
        m = 1.0
        for pat in self.patterns:
            if pat.applies(v, p, r):
                m *= pat.value(t, tz, v)
        for ov in self.overlays:
            if ov.applies(v, p, r):
                m *= ov.multiplier(t)
        return max(0.0, m)

    def targets(self, t: float) -> dict[Cohort, float]:
        return {c: b * self.multiplier(c, t) for c, b in self.base.items()}

    def total_target(self, t: float) -> float:
        return sum(self.targets(t).values())

    def effects(self, t: float) -> dict[Cohort, dict]:
        """Active overlay effects per cohort, so region-scoped overlays stay in their region."""
        return {(v, p, r): effects_for(self.overlays, v, p, t, r) for v, p, r in self.cohorts}

    def _sample_times(self, samples: int) -> list[float]:
        horizon = (self.end - self.start) if self.end else 7 * 86400
        end = self.start + horizon
        ts = [self.start + horizon * i / samples for i in range(samples + 1)]
        for ov in self.overlays:  # sample each overlay's plateau, clamped to the run window
            for t in (ov.at + ov.rise, ov.at + ov.rise + ov.hold / 2, ov.at + ov.rise + ov.hold):
                ts.append(min(max(t, self.start), end))
        return ts

    def peak_estimate(self, samples: int = 400) -> tuple[float, float]:
        """(peak total target, sim time of peak) over the scenario duration (or 7 days)."""
        best, best_t = 0.0, self.start
        for t in self._sample_times(samples):
            v = self.total_target(t)
            if v > best:
                best, best_t = v, t
        return best, best_t

    def cohort_peaks(self) -> dict[Cohort, float]:
        peaks = dict.fromkeys(self.cohorts, 0.0)
        for t in self._sample_times(200):
            for c, v in self.targets(t).items():
                peaks[c] = max(peaks[c], v)
        return peaks
