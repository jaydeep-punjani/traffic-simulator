"""Scenario + destination configuration models and loaders."""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenarios"
CATALOG_DIR = ROOT / "catalogs"
CONFIG_DIR = ROOT / "config"

VERTICAL_PLATFORMS: dict[str, list[str]] = {
    "streaming": ["web", "ios", "android"],
    "ecom": ["web", "ios", "android"],
    "gaming": ["pc", "ps5"],
}
DEFAULT_PLATFORM_MIX: dict[str, dict[str, float]] = {
    "streaming": {"web": 0.30, "ios": 0.35, "android": 0.35},
    "ecom": {"web": 0.40, "ios": 0.30, "android": 0.30},
    "gaming": {"pc": 0.55, "ps5": 0.45},
}
DEFAULT_SESSION_MINUTES = {"streaming": 35.0, "ecom": 9.0, "gaming": 55.0}

# UTC offsets drive diurnal/weekly patterns; countries/locales feed contexts.
REGIONS: dict[str, dict[str, Any]] = {
    "us": {"tz": -5.0, "countries": [("US", "en-US", "America/New_York", 0.55), ("US", "en-US", "America/Chicago", 0.2),
                                      ("US", "en-US", "America/Los_Angeles", 0.25)]},
    "eu": {"tz": 1.0, "countries": [("GB", "en-GB", "Europe/London", 0.3), ("DE", "de-DE", "Europe/Berlin", 0.3),
                                     ("FR", "fr-FR", "Europe/Paris", 0.25), ("ES", "es-ES", "Europe/Madrid", 0.15)]},
    "apac": {"tz": 8.0, "countries": [("JP", "ja-JP", "Asia/Tokyo", 0.35), ("AU", "en-AU", "Australia/Sydney", 0.25),
                                       ("SG", "en-SG", "Asia/Singapore", 0.15), ("KR", "ko-KR", "Asia/Seoul", 0.25)]},
    "latam": {"tz": -3.0, "countries": [("BR", "pt-BR", "America/Sao_Paulo", 0.6), ("MX", "es-MX", "America/Mexico_City", 0.4)]},
    "india": {"tz": 5.5, "countries": [("IN", "en-IN", "Asia/Kolkata", 1.0)]},
}


class SessionLength(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mean_minutes: float | None = None
    sigma: float = Field(0.8, gt=0, le=3)
    min_minutes: float = Field(0.5, ge=0)
    max_minutes: float = Field(480.0, gt=0)


class VerticalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    weight: float = Field(1.0, ge=0)
    platforms: dict[str, float] | None = None
    session: SessionLength = Field(default_factory=SessionLength)
    catalog: str | None = None
    event_names: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    user_pool_factor: float = Field(5.0, gt=0)


class TrafficConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_concurrency: float = Field(1000, ge=0)
    regions: dict[str, float] = Field(default_factory=lambda: {"us": 0.45, "eu": 0.30, "apac": 0.25})
    patterns: list[dict[str, Any]] = Field(default_factory=list)
    arrival_tau_seconds: float = Field(120.0, gt=0)

    @field_validator("regions")
    @classmethod
    def _regions(cls, v):
        unknown = set(v) - set(REGIONS)
        if unknown:
            raise ValueError(f"unknown regions {sorted(unknown)}; choose from {sorted(REGIONS)}")
        if sum(v.values()) <= 0:
            raise ValueError("region weights must sum to > 0")
        return v


class SdkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_size: int = Field(100, ge=1, le=2000)
    upload_interval: float = Field(0.5, gt=0)
    max_queue_size: int = Field(50000, ge=100)
    threads: int = Field(1, ge=1, le=16)
    gzip: bool = False
    timeout: float = 15
    max_retries: int = 5


class OverlayConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    at: str | float = "+0s"
    name: str | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    start_at: str | float = "now"
    duration: str | float | None = "1h"
    speed: float = Field(1.0, gt=0)
    seed: int = 42
    workers: int | None = Field(None, ge=1, le=256)
    warm_start: bool = True
    events_per_sec_cap: float | None = Field(None, gt=0)
    sample_payloads: float = Field(0.0, ge=0, le=1)
    sdk: SdkConfig = Field(default_factory=SdkConfig)
    traffic: TrafficConfig = Field(default_factory=TrafficConfig)
    verticals: dict[str, VerticalConfig]
    events: list[OverlayConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        for v, vc in self.verticals.items():
            if v not in VERTICAL_PLATFORMS:
                raise ValueError(f"unknown vertical {v!r}; choose from {sorted(VERTICAL_PLATFORMS)}")
            mix = vc.platforms or DEFAULT_PLATFORM_MIX[v]
            bad = set(mix) - set(VERTICAL_PLATFORMS[v])
            if bad:
                raise ValueError(f"vertical {v}: unsupported platforms {sorted(bad)} (allowed {VERTICAL_PLATFORMS[v]})")
            if sum(mix.values()) <= 0:
                raise ValueError(f"vertical {v}: platform weights must sum to > 0")
            vc.platforms = {p: float(w) for p, w in mix.items() if w > 0}
            if vc.session.mean_minutes is None:
                vc.session.mean_minutes = DEFAULT_SESSION_MINUTES[v]
        if not any(vc.enabled and vc.weight > 0 for vc in self.verticals.values()):
            raise ValueError("at least one vertical must be enabled with weight > 0")
        return self

    def enabled_verticals(self) -> list[str]:
        return [v for v, vc in self.verticals.items() if vc.enabled and vc.weight > 0]


# ---------------------------------------------------------------------------
# Loading / overrides
# ---------------------------------------------------------------------------

def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return yaml.safe_load(value)
        except yaml.YAMLError:
            return value
    return value


def apply_overrides(raw: dict, overrides: dict[str, Any] | None) -> dict:
    """Apply dotted-key overrides, e.g. {'traffic.base_concurrency': 5000}."""
    data = copy.deepcopy(raw)
    for key, value in (overrides or {}).items():
        parts = key.split(".")
        cur = data
        for p in parts[:-1]:
            if not isinstance(cur.get(p), dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = _coerce(value)
    return data


def parse_set_args(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--set expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = _coerce(v)
    return out


def resolve_scenario_path(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.exists():
        return p
    for cand in (SCENARIO_DIR / name_or_path, SCENARIO_DIR / f"{name_or_path}.yaml"):
        if cand.exists():
            return cand
    raise FileNotFoundError(f"scenario not found: {name_or_path}")


def load_scenario_raw(name_or_path: str) -> dict:
    with open(resolve_scenario_path(name_or_path)) as f:
        return yaml.safe_load(f) or {}


def load_scenario(name_or_path: str, overrides: dict | None = None) -> Scenario:
    return Scenario.model_validate(apply_overrides(load_scenario_raw(name_or_path), overrides))


def list_scenarios() -> list[dict]:
    out = []
    for p in sorted(SCENARIO_DIR.glob("*.yaml")):
        try:
            raw = yaml.safe_load(p.read_text()) or {}
            out.append({"id": p.stem, "name": raw.get("name", p.stem), "description": raw.get("description", ""), "raw": raw})
        except yaml.YAMLError as e:
            out.append({"id": p.stem, "name": p.stem, "description": f"YAML error: {e}", "raw": {}})
    return out


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


class Destinations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "http://localhost:8088"
    default_write_key: str = "dev-write-key"
    sources: dict[str, str] = Field(default_factory=dict)

    def write_key(self, vertical: str, platform: str) -> str:
        return self.sources.get(f"{vertical}_{platform}") or self.sources.get(vertical) or self.default_write_key

    def redacted(self) -> dict:
        def mask(k: str) -> str:
            return k[:4] + "…" if len(k) > 6 else "***"
        return {"host": self.host, "default_write_key": mask(self.default_write_key),
                "sources": {k: mask(v) for k, v in self.sources.items()}}


def load_destinations(path: str | None = None, host_override: str | None = None) -> Destinations:
    candidates = [Path(path)] if path else [CONFIG_DIR / "destinations.yaml", CONFIG_DIR / "destinations.example.yaml"]
    data: dict = {}
    for c in candidates:
        if c.exists():
            data = expand_env(yaml.safe_load(c.read_text()) or {})
            break
    else:
        if path:
            raise FileNotFoundError(path)
    data = {k: v for k, v in data.items() if v not in (None, "")}
    if "sources" in data:
        data["sources"] = {k: v for k, v in data["sources"].items() if v}
    dest = Destinations.model_validate(data)
    if host_override:
        dest.host = host_override
    return dest
