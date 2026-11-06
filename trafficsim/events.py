"""Scenario event overlays (premiere, flash sale, game launch, ...).

An overlay multiplies target concurrency for its scope with a rise/hold/decay
envelope, and while active it biases journey transitions and exposes
"featured" parameters (a premiere title, sale SKUs, a push campaign ...).
"""

from __future__ import annotations

import copy
from typing import Any

from .patterns import envelope, resolve_time
from .timeutil import parse_duration

PRESETS: dict[str, dict[str, Any]] = {
    # ---- streaming
    "premiere": {
        "vertical": "streaming",
        "rise": "20m",
        "hold": "2h",
        "decay": "3h",
        "magnitude": 2.5,
        "bias": {"play": 1.6, "browse": 0.8},
        "featured": {
            "title": "The Last Horizon",
            "asset_id": "prem-0001",
            "program": "The Last Horizon",
            "genre": "Sci-Fi",
            "share": 0.65,
            "length_minutes": 58,
        },
    },
    "live_event": {
        "vertical": "streaming",
        "rise": "8m",
        "hold": "3h",
        "decay": "20m",
        "magnitude": 4.0,
        "bias": {"play": 2.5, "search": 0.6, "browse": 0.6},
        "featured": {
            "title": "Championship Final (Live)",
            "asset_id": "live-0001",
            "program": "Championship Final",
            "genre": "Sports",
            "share": 0.85,
            "livestream": True,
            "length_minutes": 180,
        },
    },
    # ---- e-commerce
    "flash_sale": {
        "vertical": "ecom",
        "rise": "10m",
        "hold": "1h",
        "decay": "30m",
        "magnitude": 3.0,
        "bias": {"product": 1.3, "add_to_cart": 2.0, "checkout": 1.8, "payment": 1.3, "order": 1.4, "exit": 0.7},
        "featured": {"sale_name": "Lightning Deals", "featured_skus": 25, "share": 0.5, "discount": 0.4, "coupon": "FLASH40"},
    },
    "black_friday": {
        "vertical": "ecom",
        "rise": "6h",
        "hold": "3d",
        "decay": "12h",
        "magnitude": 2.2,
        "bias": {"add_to_cart": 1.6, "checkout": 1.5, "order": 1.3, "coupon": 2.0, "exit": 0.8},
        "featured": {"sale_name": "Black Friday", "featured_skus": 200, "share": 0.35, "discount": 0.3, "coupon": "BF2026"},
    },
    "campaign_push": {
        "vertical": "ecom",
        "platforms": ["ios", "android"],
        "rise": "2m",
        "hold": "15m",
        "decay": "45m",
        "magnitude": 2.0,
        "bias": {"product": 1.4, "add_to_cart": 1.3},
        "featured": {
            "campaign": {"name": "weekend_deals_push", "source": "push", "medium": "push_notification"},
            "push_open_share": 0.7,
            "featured_skus": 10,
            "share": 0.4,
            "discount": 0.15,
        },
    },
    # ---- gaming
    "game_launch": {
        "vertical": "gaming",
        "rise": "2h",
        "hold": "2d",
        "decay": "1d",
        "magnitude": 5.0,
        "bias": {"matchmaking": 1.3},
        "featured": {"game": "Starfall Odyssey", "game_id": "starfall", "share": 0.7, "new_player_share": 0.6, "build": "1.0.0"},
    },
    "patch_day": {
        "vertical": "gaming",
        "rise": "15m",
        "hold": "1h",
        "decay": "2h",
        "magnitude": 2.5,
        "bias": {"store": 1.5},
        "featured": {"patch": "Season 7 Update", "build_bump": True},
    },
    "tournament": {
        "vertical": "gaming",
        "rise": "30m",
        "hold": "4h",
        "decay": "30m",
        "magnitude": 2.0,
        "bias": {"matchmaking": 3.0, "store": 0.6, "exit": 0.6},
        "featured": {"mode": "ranked_tournament", "tournament": "Spring Invitational"},
    },
    "weekend_event": {
        "vertical": "gaming",
        "rise": "4h",
        "hold": "48h",
        "decay": "4h",
        "magnitude": 1.6,
        "bias": {"matchmaking": 1.4, "store": 1.3},
        "featured": {"mode": "double_xp", "xp_multiplier": 2.0},
    },
    # fully user-defined
    "custom": {"rise": "10m", "hold": "1h", "decay": "30m", "magnitude": 1.5, "bias": {}, "featured": {}},
}

_SHAPE_KEYS = ("rise", "hold", "decay", "magnitude")
MAX_MAGNITUDE = 100.0
MAX_OVERLAY_PHASE = 30 * 86400.0


class Overlay:
    def __init__(self, spec: dict, start: float):
        spec = dict(spec)
        kind = spec.get("type")
        if kind not in PRESETS:
            raise ValueError(f"unknown event type {kind!r}; choose from {sorted(PRESETS)}")
        merged = copy.deepcopy(PRESETS[kind])
        params = spec.pop("params", None) or {}
        for k, v in spec.items():
            if k in ("bias", "featured") and isinstance(v, dict):
                merged.setdefault(k, {}).update(v)
            else:
                merged[k] = v
        merged.setdefault("featured", {}).update(params)
        self.kind = kind
        self.name = merged.get("name") or kind
        self.vertical = merged.get("vertical")
        if self.vertical is None:
            raise ValueError(f"event {self.name}: 'vertical' is required")
        self.platforms = set(merged.get("platforms") or [])
        self.regions = set(merged.get("regions") or [])
        self.at = resolve_time(merged.get("at", "+0s"), start)
        self.rise = parse_duration(merged["rise"])
        self.hold = parse_duration(merged["hold"])
        self.decay = parse_duration(merged["decay"])
        self.magnitude = float(merged["magnitude"])
        for k in ("rise", "hold", "decay"):
            if not 0 <= getattr(self, k) <= MAX_OVERLAY_PHASE:
                raise ValueError(f"event {self.name}: {k} must be between 0 and {MAX_OVERLAY_PHASE / 86400:.0f}d")
        if not 0 <= self.magnitude <= MAX_MAGNITUDE:
            raise ValueError(f"event {self.name}: magnitude must be between 0 and {MAX_MAGNITUDE:g}")
        self.bias: dict[str, float] = {k: float(v) for k, v in (merged.get("bias") or {}).items()}
        self.featured: dict[str, Any] = merged.get("featured") or {}
        self.end = self.at + self.rise + self.hold + self.decay
        self.spec = {
            "type": kind,
            "name": self.name,
            "at": self.at,
            "vertical": self.vertical,
            "platforms": sorted(self.platforms),
            "regions": sorted(self.regions),
            **{k: getattr(self, k) for k in _SHAPE_KEYS},
            "bias": self.bias,
            "featured": self.featured,
        }

    def applies(self, vertical: str, platform: str, region: str | None = None) -> bool:
        return (
            vertical == self.vertical
            and (not self.platforms or platform in self.platforms)
            and (region is None or not self.regions or region in self.regions)
        )

    def intensity(self, t: float) -> float:
        return envelope(t, self.at, self.rise, self.hold, self.decay)

    def multiplier(self, t: float) -> float:
        return 1.0 + (self.magnitude - 1.0) * self.intensity(t)

    def active(self, t: float) -> bool:
        return self.at <= t < self.end

    def describe(self) -> dict:
        return dict(self.spec, end=self.end)


def effects_for(overlays: list[Overlay], vertical: str, platform: str, t: float, region: str | None = None) -> dict:
    """Merge active overlay effects for a (vertical, platform[, region]) cohort at sim time t.

    Bias strength scales with the overlay envelope so effects fade in/out.
    `featured` is ordered most-recently-started first, so when overlays of the
    same kind overlap the newest one wins.
    """
    bias: dict[str, float] = {}
    active: list[tuple[float, dict]] = []
    for ov in overlays:
        if not ov.applies(vertical, platform, region):
            continue
        k = ov.intensity(t)
        if k <= 0:
            continue
        for state, w in ov.bias.items():
            bias[state] = bias.get(state, 1.0) * (1.0 + (w - 1.0) * k)
        active.append((ov.at, dict(ov.featured, _kind=ov.kind, _name=ov.name, _intensity=k)))
    active.sort(key=lambda x: -x[0])
    return {"bias": bias, "featured": [f for _, f in active]}
