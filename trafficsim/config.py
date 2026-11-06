"""Scenario + destination configuration models and loaders."""

from __future__ import annotations

import copy
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PKG = Path(__file__).resolve().parent
_CHECKOUT = _PKG.parent
_BUNDLED = _PKG / "_data"  # scenarios/catalogs/config copied into the wheel (see pyproject.toml)


def _find_home() -> tuple[Path, bool]:
    """Directory holding scenarios/, catalogs/ and config/.

    Order: $TRAFFICSIM_HOME, the source checkout this package lives in, then the
    copy bundled inside an installed wheel. Returns (path, is_bundled).
    """
    env = os.environ.get("TRAFFICSIM_HOME")
    if env:
        return Path(env).expanduser().resolve(), False
    if (_CHECKOUT / "scenarios").is_dir() and (_CHECKOUT / "catalogs").is_dir():
        return _CHECKOUT, False
    return _BUNDLED, True


ROOT, BUNDLED = _find_home()
SCENARIO_DIR = ROOT / "scenarios"
CATALOG_DIR = ROOT / "catalogs"
CONFIG_DIR = ROOT / "config"
# An installed (bundled) package must not write into site-packages: runs go to ./runs.
RUNS_DIR = (
    Path(os.environ["TRAFFICSIM_RUNS_DIR"])
    if os.environ.get("TRAFFICSIM_RUNS_DIR")
    else (Path.cwd() / "runs" if BUNDLED else ROOT / "runs")
)

# Identifiers accepted where a file path must not be (API scenario ids, catalog refs).
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_BASE_CONCURRENCY = 1_000_000


def safe_load_strict(text: str) -> Any:
    """`yaml.safe_load` that first rejects aliases, so a small document cannot expand into a huge one (billion laughs)."""
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.AliasEvent):
            mark = event.start_mark if isinstance(event.start_mark, yaml.error.Mark) else None
            raise yaml.composer.ComposerError(None, None, "YAML aliases are not allowed", mark)
    return yaml.safe_load(text)


def yaml_error_message(e: yaml.YAMLError) -> str:
    """Short YAML error text without the source snippet PyYAML embeds in str(e)."""
    mark = getattr(e, "problem_mark", None)
    problem = getattr(e, "problem", None) or e.__class__.__name__
    if mark is not None:
        return f"invalid YAML: {problem} (line {mark.line + 1}, column {mark.column + 1})"
    return f"invalid YAML: {problem}"


def _iso_if_datetime(value: Any) -> Any:
    """YAML turns unquoted ISO timestamps into datetime/date objects; keep them as ISO strings."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()
    return value


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
    "us": {
        "tz": -5.0,
        "countries": [
            ("US", "en-US", "America/New_York", 0.55),
            ("US", "en-US", "America/Chicago", 0.2),
            ("US", "en-US", "America/Los_Angeles", 0.25),
        ],
    },
    "eu": {
        "tz": 1.0,
        "countries": [
            ("GB", "en-GB", "Europe/London", 0.3),
            ("DE", "de-DE", "Europe/Berlin", 0.3),
            ("FR", "fr-FR", "Europe/Paris", 0.25),
            ("ES", "es-ES", "Europe/Madrid", 0.15),
        ],
    },
    "apac": {
        "tz": 8.0,
        "countries": [
            ("JP", "ja-JP", "Asia/Tokyo", 0.35),
            ("AU", "en-AU", "Australia/Sydney", 0.25),
            ("SG", "en-SG", "Asia/Singapore", 0.15),
            ("KR", "ko-KR", "Asia/Seoul", 0.25),
        ],
    },
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
    base_concurrency: float = Field(1000, ge=0, le=MAX_BASE_CONCURRENCY)
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
    timeout: float = Field(15, gt=0)
    max_retries: int = Field(5, ge=0, le=20)


class OverlayConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    at: str | float = "+0s"
    name: str | None = None

    @field_validator("at", mode="before")
    @classmethod
    def _at_iso(cls, v):
        return _iso_if_datetime(v)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    start_at: str | float = "now"
    duration: str | float | None = "1h"
    speed: float = Field(1.0, gt=0, le=10_000)
    seed: int = 42
    workers: int | None = Field(None, ge=1, le=256)
    warm_start: bool = True
    events_per_sec_cap: float | None = Field(None, gt=0)
    sample_payloads: float = Field(0.0, ge=0, le=1)
    sdk: SdkConfig = Field(default_factory=SdkConfig)
    traffic: TrafficConfig = Field(default_factory=TrafficConfig)
    verticals: dict[str, VerticalConfig]
    events: list[OverlayConfig] = Field(default_factory=list)

    @field_validator("start_at", mode="before")
    @classmethod
    def _start_iso(cls, v):
        return _iso_if_datetime(v)

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
        from .timeutil import parse_duration, parse_start_at

        parse_start_at(self.start_at)
        if self.duration not in (None, "", 0) and parse_duration(self.duration) <= 0:
            raise ValueError("duration must be positive")
        return self

    def enabled_verticals(self) -> list[str]:
        return [v for v, vc in self.verticals.items() if vc.enabled and vc.weight > 0]


# ---------------------------------------------------------------------------
# Loading / overrides
# ---------------------------------------------------------------------------


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return _iso_if_datetime(safe_load_strict(value))
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


def _listed_yaml(directory: Path, ident: str) -> Path | None:
    """`<ident>.yaml` in `directory`, picked from a directory listing so the returned path never contains caller input."""
    if not ID_RE.match(ident):
        return None
    return next((p for p in sorted(directory.glob("*.yaml")) if p.stem == ident), None)


def bundled_scenario_path(sid: str) -> Path:
    """A scenario in scenarios/ by id. The API uses this so remote callers cannot name arbitrary files."""
    if not ID_RE.match(sid):
        raise ValueError("scenario id must match [A-Za-z0-9_-] (max 64 chars)")
    path = _listed_yaml(SCENARIO_DIR, sid)
    if path is None:
        raise FileNotFoundError(f"scenario not found: {sid}")
    return path


def bundled_catalog_path(ref: str) -> Path:
    """A catalog in catalogs/ by id. The API uses this so remote callers cannot name arbitrary files."""
    path = _listed_yaml(CATALOG_DIR, ref)
    if path is None:
        raise ValueError(f"only catalog ids from {CATALOG_DIR.name}/ are allowed here")
    return path


def resolve_scenario_path(name_or_path: str, allow_path: bool = True) -> Path:
    """Scenario id (file stem in scenarios/) or, when `allow_path`, any YAML file path."""
    if not allow_path:
        return bundled_scenario_path(name_or_path)
    path = _listed_yaml(SCENARIO_DIR, name_or_path)
    if path is not None:
        return path
    p = Path(name_or_path)
    if p.is_file():
        return p
    cand = SCENARIO_DIR / name_or_path
    if cand.is_file():
        return cand
    raise FileNotFoundError(f"scenario not found: {name_or_path}")


def resolve_catalog_path(vertical: str, ref: str | None, allow_path: bool = True) -> Path:
    """Catalog for a vertical: default catalogs/<vertical>.yaml, a catalog id, or (CLI only) a path."""
    if not ref:
        return CATALOG_DIR / f"{vertical}.yaml"
    if not allow_path:
        return bundled_catalog_path(ref)
    path = _listed_yaml(CATALOG_DIR, ref)
    if path is not None:
        return path
    p = Path(ref)
    if not p.is_absolute() and not p.exists():
        p = ROOT / p
    return p


def load_yaml_mapping(path: Path) -> dict:
    data = safe_load_strict(path.read_text())
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level must be a mapping")
    return data


def load_scenario_raw(name_or_path: str, allow_path: bool = True) -> dict:
    return load_yaml_mapping(resolve_scenario_path(name_or_path, allow_path))


def load_scenario(name_or_path: str, overrides: dict | None = None) -> Scenario:
    return Scenario.model_validate(apply_overrides(load_scenario_raw(name_or_path), overrides))


def list_scenarios() -> list[dict]:
    out = []
    for p in sorted(SCENARIO_DIR.glob("*.yaml")):
        try:
            raw = load_yaml_mapping(p)
            out.append({"id": p.stem, "name": raw.get("name", p.stem), "description": raw.get("description", ""), "raw": raw})
        except (yaml.YAMLError, ValueError):
            msg = "Invalid scenario file. Run `trafficsim validate` for details."
            out.append({"id": p.stem, "name": p.stem, "description": msg, "raw": {}})
    return out


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} (default applies when VAR is unset *or empty*, as in POSIX sh)."""
    if isinstance(value, str):

        def sub(m: re.Match) -> str:
            cur = os.environ.get(m.group(1), "")
            return cur if cur or m.group(2) is None else m.group(3)

        return _ENV_RE.sub(sub, value)
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

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", v):
            raise ValueError(f"destination host must be an http(s) URL, got {v!r} (unset environment variable?)")
        return v

    def write_key(self, vertical: str, platform: str) -> str:
        return self.sources.get(f"{vertical}_{platform}") or self.sources.get(vertical) or self.default_write_key

    def redacted(self) -> dict:
        """Safe to show in the dashboard/exports: write keys are never revealed, even partially."""

        def mask(k: str) -> str:
            return "***" if k else ""

        return {
            "host": self.host,
            "default_write_key": mask(self.default_write_key),
            "sources": {k: mask(v) for k, v in self.sources.items()},
        }


def load_destinations(path: str | None = None, host_override: str | None = None) -> Destinations:
    candidates = [Path(path)] if path else [CONFIG_DIR / "destinations.yaml", CONFIG_DIR / "destinations.example.yaml"]
    data: dict = {}
    for c in candidates:
        if c.exists():
            data = expand_env(load_yaml_mapping(c))
            break
    else:
        if path:
            raise FileNotFoundError(path)
    if "host" in data and not data["host"] and not host_override:
        raise ValueError("destinations: host is empty (is the environment variable it references set?)")
    data = {k: v for k, v in data.items() if v not in (None, "")}
    if "sources" in data:
        data["sources"] = {k: v for k, v in data["sources"].items() if v}
    if host_override:
        data["host"] = host_override
    return Destinations.model_validate(data)
